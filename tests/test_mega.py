import asyncio
import json
import os

import pytest
from Crypto.Cipher import AES
from Crypto.Util import Counter

from app.errors import SourceError
from app.sources.base import collect_files
from app.sources.mega import (
    MegaDecryptor,
    MegaSource,
    b64d,
    b64e,
    build_tree,
    decrypt_attrs,
    decrypt_node_key,
    file_key_parts,
    find_node,
    parse_link,
)

FILE_KEY = os.urandom(32)


def encrypt_ctr(file_key: bytes, data: bytes) -> bytes:
    """Cifrador independiente (Counter explícito) para contrastar con MegaDecryptor."""
    aes_key, nonce = file_key_parts(file_key)
    ctr = Counter.new(64, prefix=nonce, initial_value=0)
    return AES.new(aes_key, AES.MODE_CTR, counter=ctr).encrypt(data)


def encrypt_attrs(attrs: dict, aes_key: bytes) -> str:
    raw = b"MEGA" + json.dumps(attrs).encode()
    raw += b"\0" * (-len(raw) % 16)
    return b64e(AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).encrypt(raw))


def test_base64_url_safe_roundtrip():
    data = os.urandom(33)
    assert b64d(b64e(data)) == data


def test_descifrado_completo():
    plain = os.urandom(100_003)
    cipher = encrypt_ctr(FILE_KEY, plain)
    dec = MegaDecryptor(FILE_KEY)
    out = b"".join(dec.feed(cipher[i:i + 7001]) for i in range(0, len(cipher), 7001))
    assert out == plain


@pytest.mark.parametrize("offset", [0, 1, 15, 16, 17, 4096, 5000, 99_999])
def test_reanudar_en_cualquier_desplazamiento(offset):
    plain = os.urandom(100_003)
    cipher = encrypt_ctr(FILE_KEY, plain)
    dec = MegaDecryptor(FILE_KEY, offset)
    start = dec.aligned_start  # el origen se pide desde el límite de bloque
    assert start % 16 == 0 and start <= offset
    out = b"".join(dec.feed(cipher[i:i + 999]) for i in range(start, len(cipher), 999))
    assert out == plain[offset:]


def test_atributos_y_clave_incorrecta():
    aes_key, _ = file_key_parts(FILE_KEY)
    at = encrypt_attrs({"n": "Película ñandú.mkv"}, aes_key)
    assert decrypt_attrs(at, aes_key)["n"] == "Película ñandú.mkv"
    with pytest.raises(SourceError):
        decrypt_attrs(at, os.urandom(16))


def test_clave_de_nodo_en_carpeta():
    master = os.urandom(16)
    enc = AES.new(master, AES.MODE_ECB).encrypt(FILE_KEY)
    assert decrypt_node_key(f"USERID:{b64e(enc)}", master) == FILE_KEY
    assert decrypt_node_key("", master) is None


def test_parse_link_formatos():
    h, k32, k16 = "AbCd1234", b64e(os.urandom(32)), b64e(os.urandom(16))
    for url in (f"https://mega.nz/file/{h}#{k32}", f"https://mega.nz/#!{h}!{k32}", f"https://mega.co.nz/#!{h}!{k32}"):
        link = parse_link(url)
        assert link and link.kind == "file" and link.handle == h and len(link.key) == 32
    for url in (f"https://mega.nz/folder/{h}#{k16}", f"https://mega.nz/#F!{h}!{k16}"):
        link = parse_link(url)
        assert link and link.kind == "folder" and link.node is None and len(link.key) == 16
    sub = parse_link(f"https://mega.nz/folder/{h}#{k16}/file/ZyXw9876")
    assert sub and sub.node == "ZyXw9876" and sub.node_kind == "file"
    deep = parse_link(f"https://mega.nz/folder/{h}#{k16}/folder/ZyXw9876")
    assert deep and deep.node == "ZyXw9876" and deep.node_kind == "folder"
    assert parse_link("https://example.com/algo") is None
    assert MegaSource.matches(f"https://mega.nz/file/{h}#{k32}")


# -- construcción del árbol ----------------------------------------------------------------


def _enc_key(data: bytes, master: bytes) -> str:
    return "OWNER:" + b64e(AES.new(master, AES.MODE_ECB).encrypt(data))


def _file_node(handle: str, parent: str, file_key: bytes, master: bytes, name: str, size: int) -> dict:
    aes_key, _ = file_key_parts(file_key)
    return {
        "h": handle,
        "p": parent,
        "t": 0,
        "s": size,
        "k": _enc_key(file_key, master),
        "a": encrypt_attrs({"n": name}, aes_key),
    }


def _folder_node(handle: str, parent: str, folder_key: bytes, master: bytes, name: str) -> dict:
    return {
        "h": handle,
        "p": parent,
        "t": 1,
        "k": _enc_key(folder_key, master),
        "a": encrypt_attrs({"n": name}, folder_key),
    }


def _root(master: bytes, name: str = "mi carpeta") -> dict:
    """Nodo raíz de un share: su padre no viene en la respuesta de la API."""
    return {
        "h": "ROOT1234",
        "p": "PARENT1",
        "t": 1,
        "k": _enc_key(os.urandom(16), master),
        "a": encrypt_attrs({"n": name}, os.urandom(16)),
    }


def test_arbol_plano():
    master = os.urandom(16)
    k1, k2 = os.urandom(32), os.urandom(32)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "carpeta"),
        _file_node("FILEHND1", "ROOT1234", k1, master, "uno.txt", 10),
        _file_node("FILEHND2", "ROOT1234", k2, master, "dos.txt", 20),
    ]
    root = build_tree(nodes, master, "HANDLE")
    assert root is not None and root.name == "carpeta" and root.path == ""
    assert [c.name for c in root.children] == ["dos.txt", "uno.txt"]
    assert root.size == 30
    assert root.children[0].path == "dos.txt"
    assert root.children[0].ref.startswith("node:HANDLE:FILEHND2:")


def test_arbol_con_subcarpetas_anidadas():
    master = os.urandom(16)
    sub, deep = os.urandom(16), os.urandom(16)
    f1, f2, f3 = os.urandom(32), os.urandom(32), os.urandom(32)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "raiz"),
        _folder_node("SUBDIR1", "ROOT1234", sub, master, "trabajos"),
        _file_node("FILEHND1", "ROOT1234", f1, master, "raiz.txt", 1),
        _folder_node("SUBDIR2", "SUBDIR1", deep, master, "2026"),
        _file_node("FILEHND2", "SUBDIR1", f2, master, "nota.txt", 2),
        _file_node("FILEHND3", "SUBDIR2", f3, master, "informe.pdf", 3),
    ]
    root = build_tree(nodes, master, "HANDLE")
    assert root is not None
    # Las carpetas van primero y, dentro de cada una, los archivos en orden alfabético.
    assert [f.path for f in collect_files(root)] == [
        "trabajos/2026/informe.pdf",
        "trabajos/nota.txt",
        "raiz.txt",
    ]
    # Todas las claves se descifran con la clave del enlace (no con la del padre).
    assert find_node(root, "FILEHND3") is not None


def test_arbol_omite_subcarpeta_sin_clave():
    master = os.urandom(16)
    wrong = os.urandom(16)
    good = os.urandom(32)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "raiz"),
        _folder_node("SUBDIR1", "ROOT1234", os.urandom(16), wrong, "bloqueada"),
        _file_node("FILEHND1", "SUBDIR1", os.urandom(32), os.urandom(16), "oculto.bin", 1),
        _file_node("FILEHND2", "ROOT1234", good, master, "visible.bin", 2),
    ]
    root = build_tree(nodes, master, "HANDLE")
    assert root is not None
    assert [f.path for f in collect_files(root)] == ["visible.bin"]


def test_arbol_con_clave_incorrecta_queda_vacio():
    nodes = [_file_node("FILEHND1", "ROOT1234", FILE_KEY, os.urandom(16), "x", 1)]
    assert build_tree(nodes, os.urandom(16), "HANDLE") is None
    assert build_tree([], os.urandom(16), "HANDLE") is None


def test_arbol_con_clave_de_otro_usuario_delante():
    """El campo "k" puede traer varias entradas y la del enlace no es siempre la primera."""
    master = os.urandom(16)
    file_key = os.urandom(32)
    node = _file_node("FILEHND1", "ROOT1234", file_key, master, "uno.txt", 10)
    basura = b64e(AES.new(os.urandom(16), AES.MODE_ECB).encrypt(os.urandom(32)))
    node["k"] = f"USER:{basura}/{node['k']}"
    nodes = [_folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "raiz"), node]
    root = build_tree(nodes, master, "HANDLE")
    assert root is not None
    assert [f.path for f in collect_files(root)] == ["uno.txt"]


def test_raiz_con_clave_de_otro_usuario_delante():
    master = os.urandom(16)
    root_key = os.urandom(16)
    root_node = _folder_node("ROOT1234", "PARENT1", root_key, master, "raiz")
    basura = b64e(AES.new(os.urandom(16), AES.MODE_ECB).encrypt(os.urandom(16)))
    root_node["k"] = f"USER:{basura}/{root_node['k']}"
    nodes = [root_node, _file_node("FILEHND1", "ROOT1234", os.urandom(32), master, "uno.txt", 1)]
    root = build_tree(nodes, master, "HANDLE")
    assert root is not None and root.name == "raiz"
    assert [f.path for f in collect_files(root)] == ["uno.txt"]


def test_decrypt_node_key_filtra_por_longitud():
    master = os.urandom(16)
    key16, key32 = os.urandom(16), os.urandom(32)
    field = f"U1:{b64e(AES.new(master, AES.MODE_ECB).encrypt(key16))}/U1:{b64e(AES.new(master, AES.MODE_ECB).encrypt(key32))}"  # noqa: E501
    assert decrypt_node_key(field, master, expected_len=16) == key16
    assert decrypt_node_key(field, master, expected_len=32) == key32


# -- inspección ------------------------------------------------------------------------


def test_inspect_carpeta_devuelve_arbol(monkeypatch):
    master = os.urandom(16)
    sub = os.urandom(16)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "mis cosas"),
        _folder_node("SUBDIR1", "ROOT1234", sub, master, "sub"),
        _file_node("FILEHND1", "SUBDIR1", os.urandom(32), master, "a b.txt", 7),
    ]

    async def fake_api(_self, payload, folder=None):
        return {"f": nodes}

    monkeypatch.setattr(MegaSource, "_api", fake_api)
    insp = asyncio.run(MegaSource(None).inspect("https://mega.nz/folder/ROOT1234#" + b64e(master)))
    assert insp.files == []
    assert insp.tree is not None
    files = collect_files(insp.tree)
    assert len(files) == 1
    assert files[0].name == "a b.txt"
    assert files[0].size == 7
    assert files[0].path == "sub/a b.txt"
    assert files[0].ref.startswith("node:ROOT1234:FILEHND1:")


def test_inspect_enlace_profundo_a_subcarpeta(monkeypatch):
    master = os.urandom(16)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "raiz"),
        _folder_node("SUBDIR1", "ROOT1234", os.urandom(16), master, "elegida"),
        _file_node("FILEHND1", "SUBDIR1", os.urandom(32), master, "dentro.bin", 5),
    ]

    async def fake_api(_self, payload, folder=None):
        return {"f": nodes}

    monkeypatch.setattr(MegaSource, "_api", fake_api)
    url = "https://mega.nz/folder/ROOT1234#" + b64e(master) + "/folder/SUBDIR1"
    insp = asyncio.run(MegaSource(None).inspect(url))
    assert insp.tree is not None
    assert insp.tree.name == "elegida"
    # Las rutas son relativas a la raíz real del share, no al elemento enlazado.
    assert [f.path for f in collect_files(insp.tree)] == ["elegida/dentro.bin"]


def test_inspect_enlace_profundo_a_archivo(monkeypatch):
    master = os.urandom(16)
    nodes = [
        _folder_node("ROOT1234", "PARENT1", os.urandom(16), master, "raiz"),
        _file_node("FILEHND1", "ROOT1234", os.urandom(32), master, "uno.bin", 9),
    ]

    async def fake_api(_self, payload, folder=None):
        return {"f": nodes}

    monkeypatch.setattr(MegaSource, "_api", fake_api)
    url = "https://mega.nz/folder/ROOT1234#" + b64e(master) + "/file/FILEHND1"
    insp = asyncio.run(MegaSource(None).inspect(url))
    assert insp.tree is None
    assert len(insp.files) == 1
    assert insp.files[0].name == "uno.bin" and insp.files[0].size == 9
