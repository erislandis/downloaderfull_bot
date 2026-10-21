"""Enlaces directos, Google Drive y protección SSRF (servidores simulados con MockTransport)."""
import asyncio
import gzip
import io
import os
import zipfile

import httpx
import pytest

from app.errors import QuotaExceeded, ResumeNotSupported, SourceError, TransientError
from app.sources import SOURCES, resolve_source
from app.sources import direct, gdrive
from app.sources._guard import GuardedTransport, guarded_client, is_public_ip
from app.sources.base import BundleFile, SourceOptions, build_bundle
from app.sources.direct import DirectSource
from app.sources.gdrive import GoogleDriveSource, error_from_page, parse_confirmation, parse_link
from app.sources.mediafire import MediaFireSource
from app.sources.mega import MegaSource

PUBLIC_IP = "93.184.216.34"
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
FOLDER_ID = "1FoLdErIdAbCdEfGhIjKlMnOpQrSt0001"
SUB_ID = "1SuBfOlDeRiDaBcDeFgHiJkLmNoPqR0002"


async def public_resolver(host, port):
    return [PUBLIC_IP]


def patch_clients(monkeypatch, handler):
    """Hace que los orígenes usen un servidor simulado (con la protección SSRF activa)."""
    def factory():
        return guarded_client(httpx.MockTransport(handler), public_resolver)

    monkeypatch.setattr(direct, "guarded_client", factory)
    monkeypatch.setattr(gdrive, "guarded_client", factory)


async def read_all(agen):
    return b"".join([c async for c in agen])


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ protección SSRF


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("8.8.8.8", True), ("2001:4860:4860::8888", True), ("::ffff:8.8.8.8", True),
        ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.1", False), ("172.16.5.5", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
        ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:127.0.0.1", False),
        ("64:ff9b::7f00:1", False), ("2002:7f00:1::", False), ("no-es-ip", False),
    ],
)
def test_is_public_ip(addr, expected):
    assert is_public_ip(addr) is expected


def test_el_guardia_bloquea_destinos_internos():
    async def go():
        inner = httpx.MockTransport(lambda req: httpx.Response(200, text="secreto"))
        for url in ("http://127.0.0.1:10000/api/status", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
                    "http://localhost/admin"):
            async with httpx.AsyncClient(transport=GuardedTransport(inner)) as client:
                with pytest.raises(SourceError, match="interna"):
                    await client.get(url)
    run(go())


def test_el_guardia_bloquea_dominios_que_resuelven_a_ip_privada():
    async def go():
        async def resolver(host, port):
            return [PUBLIC_IP, "10.0.0.7"]  # una sola dirección privada basta para rechazar

        inner = httpx.MockTransport(lambda req: httpx.Response(200))
        async with httpx.AsyncClient(transport=GuardedTransport(inner, resolver)) as client:
            with pytest.raises(SourceError, match="interna"):
                await client.get("https://trampa.example.com/")
    run(go())


def test_el_guardia_revisa_cada_redireccion():
    """Un sitio público no puede redirigir a la red interna."""
    async def go():
        def handler(req):
            if req.url.host == "files.example.com":
                return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
            return httpx.Response(200, text="credenciales")

        async with guarded_client(httpx.MockTransport(handler), public_resolver) as client:
            with pytest.raises(SourceError, match="interna"):
                await client.get("https://files.example.com/x")
    run(go())


def test_dominio_inexistente_da_un_mensaje_claro():
    async def go():
        async def resolver(host, port):
            raise OSError("nombre desconocido")

        async with guarded_client(httpx.MockTransport(lambda r: httpx.Response(200)), resolver) as client:
            with pytest.raises(SourceError, match="No encuentro ese servidor"):
                await client.get("https://no-existe.example/")
    run(go())


# ------------------------------------------------------------------ enlace directo

BODY = os.urandom(3000)


def file_handler(support_range=True, extra=None, status_for=None):
    def handler(req):
        if status_for:
            return httpx.Response(status_for)
        rng = req.headers.get("Range")
        headers = {"Content-Type": "application/zip", **(extra or {})}
        if rng and support_range:
            start = int(rng.split("=")[1].split("-")[0])
            part = BODY[:1] if rng == "bytes=0-0" else BODY[start:]
            headers["Content-Range"] = f"bytes {start}-{start + len(part) - 1}/{len(BODY)}"
            return httpx.Response(206, content=part, headers=headers)
        return httpx.Response(200, content=BODY, headers=headers)
    return handler


def test_directo_inspect_y_stream_con_reanudacion(monkeypatch):
    patch_clients(monkeypatch, file_handler())

    async def go():
        src = DirectSource(None)
        insp = await src.inspect("https://files.example.com/descargas/Mi%20Juego.zip?token=abc")
        (f,) = insp.files
        assert (f.name, f.size) == ("Mi Juego.zip", len(BODY))
        assert await read_all(src.stream(f)) == BODY
        assert await read_all(src.stream(f, 1234)) == BODY[1234:]
    run(go())


def test_directo_usa_el_nombre_de_content_disposition(monkeypatch):
    cd = "attachment; filename=\"fallback.bin\"; filename*=UTF-8''Informe%20a%C3%B1o.pdf"
    patch_clients(monkeypatch, file_handler(extra={"Content-Disposition": cd}))

    async def go():
        (f,) = (await DirectSource(None).inspect("https://x.example.com/dl?id=9")).files
        assert f.name == "Informe año.pdf"
    run(go())


def test_directo_sin_extension_la_deduce_del_tipo(monkeypatch):
    patch_clients(monkeypatch, file_handler(extra={"Content-Type": "application/pdf"}))

    async def go():
        (f,) = (await DirectSource(None).inspect("https://x.example.com/descarga/12345")).files
        assert f.name == "12345.pdf"
    run(go())


def test_directo_rechaza_paginas_web(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(200, text="<html>hola</html>",
                                                         headers={"Content-Type": "text/html; charset=utf-8"}))

    async def go():
        with pytest.raises(SourceError, match="página web"):
            await DirectSource(None).inspect("https://blog.example.com/articulo")
    run(go())


def test_directo_acepta_html_marcado_como_adjunto(monkeypatch):
    patch_clients(monkeypatch, file_handler(extra={
        "Content-Type": "text/html", "Content-Disposition": 'attachment; filename="pagina.html"'}))

    async def go():
        (f,) = (await DirectSource(None).inspect("https://x.example.com/p")).files
        assert f.name == "pagina.html"
    run(go())


def test_directo_detecta_html_aunque_el_servidor_no_lo_adicione(monkeypatch):
    """Algunos servidores envían la web con tipo binario: el bot tiene que darse cuenta."""
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=b"<!DOCTYPE html><html><head><title>Area de descarga</title></head></html>",
        headers={"Content-Type": "application/octet-stream"}))

    async def go():
        with pytest.raises(SourceError, match="página web") as exc:
            await DirectSource(None).inspect("https://x.example.com/f.rar")
        assert "Area de descarga" in str(exc.value)
    run(go())


def test_directo_reintenta_sin_range_cuando_viene_una_web(monkeypatch):
    """Con "Range" el servidor devuelve su portada; sin él, el archivo de verdad."""

    def handler(req):
        if req.headers.get("Range"):
            return httpx.Response(200, text="<html><title>Portada</title></html>",
                                  headers={"Content-Type": "text/html"})
        return httpx.Response(200, content=BODY, headers={"Content-Type": "application/zip"})

    patch_clients(monkeypatch, handler)

    async def go():
        (f,) = (await DirectSource(None).inspect("https://x.example.com/f.zip")).files
        assert (f.name, f.size) == ("f.zip", len(BODY))
    run(go())


def test_directo_avisa_si_el_servidor_bloquea_al_bot(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, text="<html><title>Access Denied</title><p>Forbidden</p></html>",
        headers={"Content-Type": "text/html"}))

    async def go():
        with pytest.raises(SourceError, match="bloquea") as exc:
            await DirectSource(None).inspect("https://x.example.com/f.rar")
        assert "Access Denied" in str(exc.value)
    run(go())


def test_directo_no_sube_una_web_disfrazada_de_binario(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=b"<!DOCTYPE html><html><title>Hola</title></html>",
        headers={"Content-Type": "application/octet-stream"}))

    async def go():
        src = DirectSource(None)
        remote = direct.RemoteFile("f.rar", 40, "https://x.example.com/f.rar")
        with pytest.raises(SourceError, match="página web"):
            await read_all(src.stream(remote))
    run(go())


@pytest.mark.parametrize("status,exc", [(404, SourceError), (403, SourceError), (503, TransientError),
                                         (429, TransientError)])
def test_directo_traduce_errores_http(monkeypatch, status, exc):
    patch_clients(monkeypatch, file_handler(status_for=status))

    async def go():
        with pytest.raises(exc):
            await DirectSource(None).inspect("https://x.example.com/f.zip")
    run(go())


def test_directo_archivo_vacio(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(416, headers={"Content-Range": "bytes */0"}))

    async def go():
        with pytest.raises(SourceError, match="vacío"):
            await DirectSource(None).inspect("https://x.example.com/vacio.bin")
    run(go())


def test_directo_con_compresion_no_fia_del_content_length(monkeypatch):
    packed = gzip.compress(BODY)
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=packed, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}))

    async def go():
        (f,) = (await DirectSource(None).inspect("https://x.example.com/a.txt")).files
        assert f.size is None  # el tamaño comprimido no es el de los bytes que se subirían
    run(go())


def test_directo_sin_rangos_no_reanuda(monkeypatch):
    patch_clients(monkeypatch, file_handler(support_range=False))

    async def go():
        src = DirectSource(None)
        (f,) = (await src.inspect("https://x.example.com/a.zip")).files
        assert await read_all(src.stream(f)) == BODY
        with pytest.raises(ResumeNotSupported):
            await read_all(src.stream(f, 10))
    run(go())


def test_directo_corte_de_conexion_es_transitorio(monkeypatch):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"abc"
            raise httpx.ReadError("conexión cortada")

    patch_clients(monkeypatch, lambda req: httpx.Response(200, stream=Broken(), headers={"Content-Type": "application/zip"}))

    async def go():
        with pytest.raises(TransientError):
            await read_all(DirectSource(None).stream(direct.RemoteFile("a.zip", 10, "https://x.example.com/a.zip")))
    run(go())


# ------------------------------------------------------------------ Google Drive: enlaces


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing", ("file", FILE_ID)),
        (f"https://drive.google.com/file/u/0/d/{FILE_ID}/edit", ("file", FILE_ID)),
        (f"https://drive.google.com/open?id={FILE_ID}", ("file", FILE_ID)),
        (f"https://drive.google.com/uc?export=download&id={FILE_ID}", ("file", FILE_ID)),
        (f"https://drive.usercontent.google.com/download?id={FILE_ID}&export=download", ("file", FILE_ID)),
        (f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing", ("folder", FOLDER_ID)),
        (f"https://drive.google.com/drive/u/1/folders/{FOLDER_ID}", ("folder", FOLDER_ID)),
        (f"https://drive.google.com/embeddedfolderview?id={FOLDER_ID}", ("folder", FOLDER_ID)),
        (f"https://docs.google.com/document/d/{FILE_ID}/edit", ("doc", FILE_ID)),
        (f"https://docs.google.com/spreadsheets/d/{FILE_ID}/edit#gid=0", ("doc", FILE_ID)),
    ],
)
def test_parse_link(url, expected):
    link = parse_link(url)
    assert link is not None and (link.kind, link.id) == expected


@pytest.mark.parametrize("url", [
    "https://drive.google.com/drive/my-drive", "https://drive.google.com/", "https://example.com/file/d/" + FILE_ID,
    "https://drive.google.com/uc?id=corto"])
def test_parse_link_rechaza_lo_que_no_es_un_archivo(url):
    assert parse_link(url) is None


def test_resolve_source_elige_el_origen_correcto():
    assert isinstance(resolve_source("https://www.mediafire.com/file/abc/x.zip/file", None), MediaFireSource)
    assert isinstance(resolve_source(f"https://mega.nz/file/abcdefgh#{'A' * 43}", None), MegaSource)
    assert isinstance(resolve_source(f"https://drive.google.com/file/d/{FILE_ID}/view", None), GoogleDriveSource)
    assert isinstance(resolve_source("https://cdn.example.com/archivo.zip", None), DirectSource)
    assert resolve_source("ftp://cdn.example.com/archivo.zip", None) is None
    assert SOURCES[-1] is DirectSource  # acepta cualquier http(s): tiene que ir el último


def test_enlace_de_google_irreconocible_da_un_mensaje_claro(monkeypatch):
    async def go():
        with pytest.raises(SourceError, match="No reconozco"):
            await GoogleDriveSource(None).inspect("https://drive.google.com/drive/my-drive")
    run(go())


# ------------------------------------------------------------------ Google Drive: sin clave

WARNING_PAGE = f"""<html><body><p>Google Drive can't scan this file for viruses.</p>
<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
<input type="hidden" name="id" value="{FILE_ID}"><input type="hidden" name="export" value="download">
<input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="abc-123">
<input type="hidden" name="at" value="TOKEN&amp;x"><input type="submit" value="Download anyway"></form>
</body></html>"""
HTML = {"Content-Type": "text/html; charset=utf-8"}
DISPOSITION = "attachment; filename=\"grande.bin\"; filename*=UTF-8''grande.bin"


def test_parse_confirmation_lee_el_formulario():
    url, fields = parse_confirmation(WARNING_PAGE)
    assert url == "https://drive.usercontent.google.com/download"
    assert fields == {"id": FILE_ID, "export": "download", "confirm": "t", "uuid": "abc-123", "at": "TOKEN&x"}


def test_parse_confirmation_enlace_antiguo_y_json():
    old = '<a href="/uc?export=download&amp;confirm=AbC1&amp;id=XYZ">Download anyway</a>'
    assert parse_confirmation(old) == ("https://drive.google.com/uc?export=download&confirm=AbC1&id=XYZ", {})
    js = '..."downloadUrl":"https:\\/\\/drive.usercontent.google.com\\/download?id\\u003dQ\\u0026confirm\\u003dt"...'
    assert parse_confirmation(js)[0] == "https://drive.usercontent.google.com/download?id=Q&confirm=t"
    assert parse_confirmation("<html>nada</html>") is None


def test_error_from_page():
    assert isinstance(error_from_page("Sorry, you can't view or download this file at this time. "
                                      "Too many users have viewed or downloaded this file recently."), QuotaExceeded)
    assert "no es público" in str(error_from_page("<h1>You need access</h1> Request access"))
    assert "no existe" in str(error_from_page("<html>cualquier cosa</html>", 404))
    sub = '<p class="uc-error-subcaption">Archivo en cuarentena &amp; bloqueado</p>'
    assert "cuarentena & bloqueado" in str(error_from_page(sub))
    assert error_from_page("<html>normal</html>") is None


def drive_keyless_handler(seen):
    def handler(req):
        seen.append(req)
        assert req.url.host == "drive.usercontent.google.com"
        if "uuid" not in req.url.params:
            return httpx.Response(200, text=WARNING_PAGE, headers=HTML)
        assert req.url.params["at"] == "TOKEN&x"
        rng = req.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            part = BODY[start:]
            return httpx.Response(206, content=part, headers={
                "Content-Disposition": DISPOSITION, "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"})
        return httpx.Response(200, content=BODY, headers={
            "Content-Disposition": DISPOSITION, "Content-Type": "application/octet-stream"})
    return handler


def test_drive_sin_clave_salta_el_aviso_de_virus(monkeypatch):
    seen = []
    patch_clients(monkeypatch, drive_keyless_handler(seen))

    async def go():
        src = GoogleDriveSource(None)
        insp = await src.inspect(f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing")
        (f,) = insp.files
        assert (f.name, f.size, f.ref) == ("grande.bin", len(BODY), f"file:{FILE_ID}")
        assert await read_all(src.stream(f)) == BODY
        assert await read_all(src.stream(f, 500)) == BODY[500:]
    run(go())
    assert all(r.url.params["id"] == FILE_ID for r in seen)


def test_drive_sin_clave_archivo_pequeno_sin_aviso(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=BODY, headers={"Content-Disposition": DISPOSITION, "Content-Type": "application/zip"}))

    async def go():
        (f,) = (await GoogleDriveSource(None).inspect(f"https://drive.google.com/open?id={FILE_ID}")).files
        assert (f.name, f.size) == ("grande.bin", len(BODY))
    run(go())


def test_drive_sin_clave_cuota_agotada(monkeypatch):
    page = "<html>Sorry, you can't view or download this file at this time. Too many users have viewed or downloaded this file recently.</html>"
    patch_clients(monkeypatch, lambda req: httpx.Response(200, text=page, headers=HTML))

    async def go():
        with pytest.raises(QuotaExceeded, match="limitó"):
            await GoogleDriveSource(None).inspect(f"https://drive.google.com/file/d/{FILE_ID}/view")
    run(go())


def test_drive_sin_clave_archivo_privado(monkeypatch):
    page = "<html><h1>You need access</h1><button>Request access</button></html>"
    patch_clients(monkeypatch, lambda req: httpx.Response(200, text=page, headers=HTML))

    async def go():
        with pytest.raises(SourceError, match="no es público"):
            await GoogleDriveSource(None).inspect(f"https://drive.google.com/file/d/{FILE_ID}/view")
    run(go())


def test_drive_sin_clave_no_se_cuelga_si_el_aviso_se_repite(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(200, text=WARNING_PAGE, headers=HTML))

    async def go():
        with pytest.raises(SourceError, match="verificación"):
            await GoogleDriveSource(None).inspect(f"https://drive.google.com/file/d/{FILE_ID}/view")
    run(go())


def test_drive_sin_clave_rechaza_carpetas_con_instrucciones():
    async def go():
        with pytest.raises(SourceError, match="GOOGLE_API_KEY"):
            await GoogleDriveSource(None).inspect(f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    run(go())


def test_drive_exporta_documentos_nativos(monkeypatch):
    def handler(req):
        assert req.url.host == "docs.google.com" and req.url.path == f"/document/d/{FILE_ID}/export"
        assert req.url.params["format"] == "docx"
        return httpx.Response(200, content=BODY, headers={
            "Content-Disposition": "attachment; filename*=UTF-8''Mi%20documento.docx", "Content-Type": "application/octet-stream"})
    patch_clients(monkeypatch, handler)

    async def go():
        src = GoogleDriveSource(None)
        (f,) = (await src.inspect(f"https://docs.google.com/document/d/{FILE_ID}/edit")).files
        assert (f.name, f.ref) == ("Mi documento.docx", f"export:document:{FILE_ID}")
        assert await read_all(src.stream(f)) == BODY
    run(go())


# ------------------------------------------------------------------ Google Drive: con clave

KEY = SourceOptions(google_api_key="CLAVE-DE-PRUEBA")
DOC_ID = "1DoCiDaBcDeFgHiJkLmNoPqRsTuV00003"


def api_handler(seen=None):
    meta = {
        FOLDER_ID: {"id": FOLDER_ID, "name": "Proyecto", "mimeType": gdrive.FOLDER_MIME},
        FILE_ID: {"id": FILE_ID, "name": "video.mp4", "size": "123456", "mimeType": "video/mp4"},
    }
    children = {
        FOLDER_ID: [
            {"id": "f1" * 6, "name": "a.txt", "size": "5", "mimeType": "text/plain"},
            {"id": SUB_ID, "name": "Sub", "mimeType": gdrive.FOLDER_MIME},
            {"id": DOC_ID, "name": "Informe", "mimeType": "application/vnd.google-apps.document"},
            {"id": "fm" * 6, "name": "Encuesta", "mimeType": "application/vnd.google-apps.form"},
            {"id": "f2" * 6, "name": "a.txt", "size": "7", "mimeType": "text/plain"},
        ],
        SUB_ID: [{"id": "f3" * 6, "name": "b.bin", "size": "10", "mimeType": "application/octet-stream"}],
    }
    contents = {"f1" * 6: b"aaaaa", "f2" * 6: b"bbbbbbb", "f3" * 6: b"0123456789"}

    def handler(req):
        if seen is not None:
            seen.append(req)
        if req.url.host == "docs.google.com":  # exportación del documento nativo
            return httpx.Response(200, content=b"DOCX!", headers={"Content-Type": "application/octet-stream"})
        assert req.url.host == "www.googleapis.com"
        assert req.url.params["key"] == "CLAVE-DE-PRUEBA"
        path = req.url.path
        if path == "/drive/v3/files":  # listado
            parent = req.url.params["q"].split("'")[1]
            return httpx.Response(200, json={"files": children[parent]})
        file_id = path.rsplit("/", 1)[-1]
        if req.url.params.get("alt") == "media":
            if file_id == "bloqueado":
                return httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "downloadQuotaExceeded"}]}})
            data = contents.get(file_id, BODY)
            rng = req.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                return httpx.Response(206, content=data[start:], headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
            return httpx.Response(200, content=data)
        if file_id in meta:
            return httpx.Response(200, json=meta[file_id])
        return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
    return handler


def test_drive_con_clave_archivo_suelto_y_reanudacion(monkeypatch):
    seen = []
    patch_clients(monkeypatch, api_handler(seen))

    async def go():
        src = GoogleDriveSource(None, 1, KEY)
        (f,) = (await src.inspect(f"https://drive.google.com/file/d/{FILE_ID}/view")).files
        assert (f.name, f.size, f.ref) == ("video.mp4", 123456, f"file:{FILE_ID}")
        assert await read_all(src.stream(f)) == BODY
        assert await read_all(src.stream(f, 100)) == BODY[100:]
    run(go())
    assert any(r.url.params.get("alt") == "media" and r.url.params.get("supportsAllDrives") == "true" for r in seen)


def test_drive_con_clave_carpeta_en_arbol_y_zip(monkeypatch):
    patch_clients(monkeypatch, api_handler())

    async def go():
        src = GoogleDriveSource(None, 1, KEY)
        insp = await src.inspect(f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing")
        tree = insp.tree
        assert tree is not None and tree.name == "Proyecto" and tree.path == ""
        assert [c.name for c in tree.children] == ["a.txt", "Sub", "Informe.docx", "a (2).txt"]  # el formulario no aparece
        sub = tree.children[1]
        assert sub.is_dir and sub.path == "Sub" and [(c.name, c.path, c.size) for c in sub.children] == [("b.bin", "Sub/b.bin", 10)]
        assert tree.size == 5 + 10 + 7  # el documento nativo no tiene tamaño conocido

        bundle = build_bundle(tree, "Proyecto.zip")
        assert isinstance(bundle, BundleFile)
        data = await read_all(src.stream(bundle))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.testzip() is None
            assert {n: zf.read(n) for n in zf.namelist()} == {
                "Proyecto/a.txt": b"aaaaa", "Proyecto/Sub/b.bin": b"0123456789",
                "Proyecto/Informe.docx": b"DOCX!", "Proyecto/a (2).txt": b"bbbbbbb"}
    run(go())


def test_drive_con_clave_traduce_errores(monkeypatch):
    patch_clients(monkeypatch, api_handler())

    async def go():
        src = GoogleDriveSource(None, 1, KEY)
        with pytest.raises(SourceError, match="no existe o no es público"):
            await src.inspect("https://drive.google.com/file/d/1NoExisteNoExisteNoExiste0000/view")
        with pytest.raises(QuotaExceeded):
            await read_all(src.stream(gdrive.RemoteFile("x", 5, "file:bloqueado")))
    run(go())


def test_api_error_clave_invalida_y_limites():
    bad_key = '{"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","errors":[{"reason":"badRequest"}]}}'
    assert "clave de API" in str(gdrive.api_error(400, bad_key))
    assert isinstance(gdrive.api_error(403, '{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'), TransientError)
    assert isinstance(gdrive.api_error(503, ""), TransientError)
    assert "no es público" in str(gdrive.api_error(403, '{"error":{"errors":[{"reason":"forbidden"}]}}'))
    assert isinstance(gdrive.api_error(500, "no es json"), TransientError)


def test_drive_con_clave_limita_el_tamano_del_arbol(monkeypatch):
    monkeypatch.setattr(gdrive, "MAX_TREE_NODES", 3)
    patch_clients(monkeypatch, api_handler())

    async def go():
        with pytest.raises(SourceError, match="demasiados elementos"):
            await GoogleDriveSource(None, 1, KEY).inspect(f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    run(go())


@pytest.mark.parametrize(
    "header,expected",
    [
        ("attachment; filename=\"a.zip\"", "a.zip"),
        ("attachment; filename=a.zip", "a.zip"),
        ("attachment; filename=\"Ano.pdf\"; filename*=UTF-8''A%C3%B1o%20nuevo.pdf", "Año nuevo.pdf"),
        ("attachment; filename*=iso-8859-1''Caf%E9.txt", "Café.txt"),
        ("attachment; filename*=raro-charset''x%20y.txt; filename=\"y.txt\"", "y.txt"),
        ("inline", None),
        ("", None),
        (None, None),
    ],
)
def test_filename_from_disposition(header, expected):
    from app.sources._http import filename_from_disposition
    assert filename_from_disposition(header) == expected


# ------------------------------------------------------------------ transporte real (TCP)


def test_enlace_directo_por_tcp_real_con_reanudacion(monkeypatch):
    """Ejercita el transporte real de httpx (no el simulado): redirección, Range y descarga."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = os.urandom(200_000)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silencio
            pass

        def do_GET(self):
            if self.path == "/ir":
                self.send_response(302)
                self.send_header("Location", "/Datos%20raros.bin")
                self.end_headers()
                return
            rng = self.headers.get("Range")
            start, body, status = 0, payload, 200
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                body = payload[:1] if rng == "bytes=0-0" else payload[start:]
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("app.sources._guard.is_public_ip", lambda addr: True)  # el servidor es local
    try:
        async def go():
            src = DirectSource(None)
            (f,) = (await src.inspect(f"http://127.0.0.1:{server.server_port}/ir")).files
            assert (f.name, f.size) == ("Datos raros.bin", len(payload))
            assert await read_all(src.stream(f)) == payload
            assert await read_all(src.stream(f, 150_000)) == payload[150_000:]
        run(go())
    finally:
        server.shutdown()
