import json
import os

import pytest
from Crypto.Cipher import AES
from Crypto.Util import Counter

from app.errors import SourceError
from app.sources.mega import (
    MegaDecryptor, MegaSource, b64d, b64e, decrypt_attrs, decrypt_node_key, file_key_parts, parse_link,
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
