import base64

from app.sources.mediafire import MediaFireSource, parse_direct_url, parse_error, parse_filename

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


def test_sin_enlace():
    assert parse_direct_url("<html>nada</html>") is None


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
