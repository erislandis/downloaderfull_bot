"""Flujos HTTP completos contra servidores simulados (httpx.MockTransport)."""
import asyncio
import base64
import json
import os

import httpx
import pytest

from app.drive import DriveFile, UploadSession
from app.errors import QuotaExceeded, Realign, ResumeNotSupported, TransientError
from app.sources.base import RemoteFile
from app.sources.mediafire import MediaFireSource
from app.sources.mega import MegaSource, b64e
from tests.test_mega import encrypt_attrs, encrypt_ctr, file_key_parts


def client(handler, **kw):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


async def read_all(agen):
    return b"".join([c async for c in agen])


# ---------------------------------------------------------------- Drive (subida reanudable)

def make_session(handler):
    return UploadSession(client(handler), "https://upload.example/session", "f.bin")


def test_drive_chunk_intermedio_y_final():
    seen = []

    def handler(req):
        seen.append(req.headers["Content-Range"])
        if req.headers["Content-Range"] == "bytes 6-9/10":
            return httpx.Response(200, json={"id": "FILEID"})
        return httpx.Response(308, headers={"Range": "bytes=0-5"})

    s = make_session(handler)
    assert asyncio.run(s.send(0, b"a" * 6, False, 10)) is None
    done = asyncio.run(s.send(6, b"b" * 4, True, 10))
    assert done == DriveFile("FILEID", "f.bin", 10)
    assert seen == ["bytes 0-5/10", "bytes 6-9/10"]


def test_drive_tamano_desconocido_usa_asterisco_y_total_en_el_ultimo():
    seen = []

    def handler(req):
        seen.append(req.headers["Content-Range"])
        return httpx.Response(200, json={"id": "X"}) if "/10" in seen[-1] else httpx.Response(308, headers={"Range": "bytes=0-5"})

    s = make_session(handler)
    asyncio.run(s.send(0, b"a" * 6, False, None))
    asyncio.run(s.send(6, b"b" * 4, True, None))
    assert seen == ["bytes 0-5/*", "bytes 6-9/10"]


def test_drive_realinea_si_guardo_menos():
    s = make_session(lambda req: httpx.Response(308, headers={"Range": "bytes=0-2"}))
    with pytest.raises(Realign) as exc:
        asyncio.run(s.send(0, b"a" * 6, False, 10))
    assert exc.value.offset == 3


def test_drive_consulta_de_estado_y_errores():
    assert asyncio.run(make_session(lambda r: httpx.Response(308)).query_offset()) == 0
    assert asyncio.run(make_session(lambda r: httpx.Response(308, headers={"Range": "bytes=0-99"})).query_offset()) == 100
    with pytest.raises(ResumeNotSupported):
        asyncio.run(make_session(lambda r: httpx.Response(404)).query_offset())
    with pytest.raises(TransientError):
        asyncio.run(make_session(lambda r: httpx.Response(503)).send(0, b"x", True, 1))


# ---------------------------------------------------------------- MediaFire

PAGE = (
    '<div class="filename">Mi Juego.zip</div>'
    '<a href="https://download9.mediafire.com/k/q/Mi%20Juego.zip" id="downloadButton">Download</a>'
)
BODY = os.urandom(5000)


def mediafire_handler(support_range=True):
    def handler(req):
        if req.url.host == "www.mediafire.com":
            return httpx.Response(200, text=PAGE)
        rng = req.headers.get("Range")
        if rng and support_range:
            start = int(rng.split("=")[1].split("-")[0])
            part = BODY[start:] if rng != "bytes=0-0" else BODY[:1]
            end = start + len(part) - 1
            return httpx.Response(206, content=part, headers={"Content-Range": f"bytes {start}-{end}/{len(BODY)}"})
        return httpx.Response(200, content=BODY, headers={"Content-Length": str(len(BODY))})
    return handler


def test_mediafire_inspect_y_stream_con_reanudacion():
    async def go():
        async with client(mediafire_handler(), follow_redirects=True) as http:
            src = MediaFireSource(http)
            [f] = await src.inspect("https://www.mediafire.com/file/abc/Mi_Juego.zip/file")
            assert (f.name, f.size) == ("Mi Juego.zip", len(BODY))
            assert await read_all(src.stream(f)) == BODY
            assert await read_all(src.stream(f, 1234)) == BODY[1234:]
    asyncio.run(go())


def test_mediafire_sin_soporte_de_rangos_pide_reiniciar():
    async def go():
        async with client(mediafire_handler(support_range=False), follow_redirects=True) as http:
            src = MediaFireSource(http)
            f = RemoteFile("x", len(BODY), "https://www.mediafire.com/file/abc/x/file")
            with pytest.raises(ResumeNotSupported):
                await read_all(src.stream(f, 100))
    asyncio.run(go())


# ---------------------------------------------------------------- Mega (API + descarga + descifrado)

def mega_world(quota=False):
    key = os.urandom(32)
    plain = os.urandom(70_001)
    cipher = encrypt_ctr(key, plain)
    aes_key, _ = file_key_parts(key)
    at = encrypt_attrs({"n": "video:raro?.mp4"}, aes_key)

    def handler(req):
        if req.url.host == "g.api.mega.co.nz":
            item = json.loads(req.content)[0]
            resp = {"s": len(plain), "at": at}
            if item.get("g"):
                resp["g"] = "https://gfs.example/dl/abc"
            return httpx.Response(200, json=[resp])
        if quota:
            return httpx.Response(509, headers={"X-MEGA-Time-Left": "1800"})
        rng = req.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(206, content=cipher[start:])
        return httpx.Response(200, content=cipher)

    return key, plain, handler


def test_mega_inspect_stream_y_reanudacion_desde_cualquier_byte():
    key, plain, handler = mega_world()
    url = f"https://mega.nz/file/AbCd1234#{b64e(key)}"

    async def go():
        async with client(handler) as http:
            src = MegaSource(http)
            [f] = await src.inspect(url)
            assert f.name == "video_raro_.mp4" and f.size == len(plain)  # nombre saneado
            assert await read_all(src.stream(f)) == plain
            for offset in (1, 16, 31_337):
                assert await read_all(src.stream(f, offset)) == plain[offset:]
    asyncio.run(go())


def test_mega_cuota_agotada_informa_tiempo_de_espera():
    key, _, handler = mega_world(quota=True)
    url = f"https://mega.nz/file/AbCd1234#{b64e(key)}"

    async def go():
        async with client(handler) as http:
            src = MegaSource(http)
            [f] = await src.inspect(url)
            with pytest.raises(QuotaExceeded) as exc:
                await read_all(src.stream(f))
            assert exc.value.retry_after == 1800
    asyncio.run(go())


def test_mega_carpeta_lista_archivos_y_descarga_uno():
    from Crypto.Cipher import AES
    master = os.urandom(16)
    key = os.urandom(32)
    plain = os.urandom(3000)
    cipher = encrypt_ctr(key, plain)
    aes_key, _ = file_key_parts(key)
    enc_key = b64e(AES.new(master, AES.MODE_ECB).encrypt(key))
    nodes = [
        {"h": "FILEHNDL", "t": 0, "s": len(plain), "k": f"USER:{enc_key}", "a": encrypt_attrs({"n": "a.bin"}, aes_key)},
        {"h": "DIRHANDL", "t": 1, "k": "USER:" + b64e(AES.new(master, AES.MODE_ECB).encrypt(os.urandom(16))), "a": ""},
    ]

    def handler(req):
        if req.url.host == "g.api.mega.co.nz":
            assert req.url.params["n"] == "FOLDERHD"  # las peticiones de carpeta llevan el handle
            item = json.loads(req.content)[0]
            if item["a"] == "f":
                return httpx.Response(200, json=[{"f": nodes}])
            return httpx.Response(200, json=[{"g": "https://gfs.example/x", "s": len(plain)}])
        return httpx.Response(200, content=cipher)

    async def go():
        async with client(handler) as http:
            src = MegaSource(http)
            files = await src.inspect(f"https://mega.nz/folder/FOLDERHD#{b64e(master)}")
            assert [(f.name, f.size) for f in files] == [("a.bin", len(plain))]
            assert await read_all(src.stream(files[0])) == plain
    asyncio.run(go())
