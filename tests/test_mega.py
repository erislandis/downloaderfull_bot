import asyncio
import json
import os

import pytest
from Crypto.Cipher import AES
from Crypto.Util import Counter

from app.errors import SourceError
from app.sources.mega import (
    MegaDecryptor, MegaSource, b64d, b64e, decrypt_attrs, decrypt_node_key, file_key_parts, parse_link,
    walk_folder,
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
    assert sub and sub.node == "ZyXw9876"
    assert parse_link("https://example.com/algo") is None
    assert MegaSource.matches(f"https://mega.nz/file/{h}#{k32}")


# -- recorrido de carpetas (walk_folder) ------------------------------------------------


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


def test_walk_folder_plana():
    root = os.urandom(16)
    k1, k2 = os.urandom(32), os.urandom(32)
    nodes = [
        _file_node("F1", "ROOT", k1, root, "uno.txt", 10),
        _file_node("F2", "ROOT", k2, root, "dos.txt", 20),
    ]
    found = walk_folder(nodes, "ROOT", root)
    assert [(p, h, s) for p, h, s, _ in found] == [("uno.txt", "F1", 10), ("dos.txt", "F2", 20)]
    assert found[0][3] == k1 and found[1][3] == k2


def test_walk_folder_subcarpetas_anidadas():
    root = os.urandom(16)
    sub, deep = os.urandom(16), os.urandom(16)
    f1, f2, f3 = os.urandom(32), os.urandom(32), os.urandom(32)
    nodes = [
        _folder_node("S1", "ROOT", sub, root, "trabajos"),
        _file_node("F1", "ROOT", f1, root, "raiz.txt", 1),
        _folder_node("S2", "S1", deep, sub, "2026"),
        _file_node("F2", "S1", f2, sub, "nota.txt", 2),
        _file_node("F3", "S2", f3, deep, "informe.pdf", 3),
    ]
    found = walk_folder(nodes, "ROOT", root)
    assert [p for p, *_ in found] == ["raiz.txt", "trabajos/nota.txt", "trabajos/2026/informe.pdf"]
    assert found[1][3] == f2 and found[2][3] == f3  # claves resueltas por jerarquía


def test_walk_folder_solo_un_nodo_profundo():
    root = os.urandom(16)
    sub = os.urandom(16)
    f1, f2 = os.urandom(32), os.urandom(32)
    nodes = [
        _folder_node("S1", "ROOT", sub, root, "carpeta"),
        _file_node("F1", "S1", f2, sub, "elegido.bin", 5),
        _file_node("F2", "ROOT", f1, root, "otro.bin", 6),
    ]
    found = walk_folder(nodes, "ROOT", root, only_node="F1")
    assert [(p, h) for p, h, *_ in found] == [("carpeta/elegido.bin", "F1")]


def test_walk_folder_omite_subcarpeta_sin_clave():
    root = os.urandom(16)
    wrong = os.urandom(16)
    good = os.urandom(32)
    nodes = [
        _folder_node("S1", "ROOT", os.urandom(16), wrong, "bloqueada"),  # clave cifrada con otra raíz
        _file_node("F1", "S1", os.urandom(32), os.urandom(16), "oculto.bin", 1),
        _file_node("F2", "ROOT", good, root, "visible.bin", 2),
    ]
    found = walk_folder(nodes, "ROOT", root)
    assert [(p, h) for p, h, *_ in found] == [("visible.bin", "F2")]


def test_walk_folder_clave_incorrecta_devuelve_vacio():
    assert walk_folder([_file_node("F1", "ROOT", FILE_KEY, os.urandom(16), "x", 1)], "ROOT", os.urandom(16)) == []
    assert walk_folder([], "ROOT", os.urandom(16)) == []


def test_decrypt_node_key_filtra_por_longitud():
    master = os.urandom(16)
    key16, key32 = os.urandom(16), os.urandom(32)
    field = f"U1:{b64e(AES.new(master, AES.MODE_ECB).encrypt(key16))}/U1:{b64e(AES.new(master, AES.MODE_ECB).encrypt(key32))}"  # noqa: E501
    assert decrypt_node_key(field, master, expected_len=16) == key16
    assert decrypt_node_key(field, master, expected_len=32) == key32


def test_inspect_folder_rutas_se_sanitizan(monkeypatch):
    root = os.urandom(16)
    sub = os.urandom(16)
    nodes = [
        _folder_node("S1", "ROOT1234", sub, root, "mis cosas"),
        _file_node("F1", "S1", os.urandom(32), sub, "a b.txt", 7),
    ]

    async def fake_api(_self, payload, folder=None):
        return {"f": nodes}

    monkeypatch.setattr(MegaSource, "_api", fake_api)
    files = asyncio.run(MegaSource(None).inspect("https://mega.nz/folder/ROOT1234#" + b64e(root)))
    assert len(files) == 1
    assert files[0].name == "mis cosas_a b.txt"
    assert files[0].size == 7
    assert files[0].ref.startswith("node:ROOT1234:F1:")
