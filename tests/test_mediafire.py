import asyncio
import base64

import httpx
import pytest

from app.errors import SourceError
from app.sources.mediafire import (
    MediaFireSource,
    _is_cloudflare_challenge,
    parse_direct_url,
    parse_error,
    parse_filename,
)

DIRECT = "https://download1234.mediafire.com/abc123/key9/Mi%20Archivo.zip"


def test_href_directo():
    page = f'<a class="input" aria-label="Download file" href="{DIRECT}" id="downloadButton">Download</a>'
    assert parse_direct_url(page) == DIRECT


def test_url_ofuscada_en_base64():
    scrambled = base64.b64encode(DIRECT.encode()).decode()
    page = f'<a href="javascript:void(0)" id="downloadButton" data-scrambled-url="{scrambled}">Download</a>'
    assert parse_direct_url(page) == DIRECT


def test_respaldo_por_regex_si_cambia_el_boton():
    page = f'<script>var u = "{DIRECT}";</script>'
    assert parse_direct_url(page) == DIRECT


def test_respaldo_protocolo_relativo_y_json():
    page = '<script>var u = "//download1234.mediafire.com/abc/key/x.zip";</script>'
    assert parse_direct_url(page) == "https://download1234.mediafire.com/abc/key/x.zip"
    page = '{"downloadUrl":"https:\\/\\/download99.mediafire.com\\/a\\/b\\/x.zip"}'
    assert parse_direct_url(page) == "https://download99.mediafire.com/a/b/x.zip"


def test_href_blob_se_ignora_y_usa_el_script():
    page = (
        '<a id="downloadButton" href="blob:https://www.mediafire.com/xxx">x</a>'
        f'<script>downloadButton.href = "{DIRECT}";</script>'
    )
    assert parse_direct_url(page) == DIRECT


def test_sin_enlace():
    assert parse_direct_url("<html>nada</html>") is None


def test_detecta_el_reto_de_cloudflare_sin_falsos_positivos():
    assert _is_cloudflare_challenge("<html><title>Just a moment...</title></html>")
    assert not _is_cloudflare_challenge(
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    )


def test_cloudflare_se_informa_como_bloqueo_de_ip():
    def handler(req):
        return httpx.Response(403, text="<html><title>Just a moment...</title></html>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = MediaFireSource(http)
            with pytest.raises(SourceError, match="MEDIAFIRE_PROXY_URL"):
                await src.inspect("https://www.mediafire.com/file/abc/x.zip/file")

    asyncio.run(go())


def test_nombre_desde_html_y_desde_url():
    assert parse_filename('<div class="filename">Mi &amp; Archivo.zip</div>', DIRECT) == "Mi & Archivo.zip"
    assert parse_filename("", DIRECT) == "Mi Archivo.zip"


def test_detecta_paginas_de_error():
    assert "peligroso" in parse_error("<h2>Dangerous File Blocked</h2>")
    assert parse_error("<html>ok</html>") is None


def test_matches():
    assert MediaFireSource.matches("https://www.mediafire.com/file/abc/x.zip/file")
    assert MediaFireSource.matches("https://download1.mediafire.com/a/b/c.zip")
    assert not MediaFireSource.matches("https://evil.com/mediafire.com")
    assert not MediaFireSource.matches("https://notmediafire.com/file/x")
