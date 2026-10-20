import base64

from app.sources.mediafire import (
    MediaFireSource,
    parse_deferred_token,
    parse_direct_url,
    parse_error,
    parse_filename,
    parse_post_data,
    quickkey,
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


def test_respaldo_protocolo_relativo():
    page = '<script>var u = "//download1234.mediafire.com/abc/key/x.zip";</script>'
    assert parse_direct_url(page) == "https://download1234.mediafire.com/abc/key/x.zip"


def test_respaldo_json_download_url():
    page = '{"downloadUrl":"https:\\/\\/download99.mediafire.com\\/a\\/b\\/x.zip"}'
    assert parse_direct_url(page) == "https://download99.mediafire.com/a/b/x.zip"


def test_scrambled_urlsafe():
    scrambled = base64.urlsafe_b64encode(DIRECT.encode()).decode().rstrip("=")
    page = f'<a id="downloadButton" data-scrambled-url="{scrambled}">x</a>'
    assert parse_direct_url(page) == DIRECT


def test_href_blob_se_ignora_y_usa_respaldo():
    page = (
        '<a id="downloadButton" href="blob:https://www.mediafire.com/xxx">x</a>'
        f'<script>downloadButton.href = "{DIRECT}";</script>'
    )
    assert parse_direct_url(page) == DIRECT


def test_sin_enlace():
    assert parse_direct_url("<html>nada</html>") is None


def test_token_de_descarga_diferida():
    page = (
        '<button type="button" id="deferredDownloadButton" '
        'data-security-token="abc.def.ghi" disabled>Download Anyway</button>'
    )
    assert parse_deferred_token(page) == "abc.def.ghi"
    assert parse_deferred_token('<a id="downloadButton" href="x">x</a>') is None


def test_post_data_del_aviso_de_malware():
    page = (
        '<button type="button" id="deferredDownloadButton" data-security-token="abc">x</button>'
        "<script>window.handleDeferredDownload("
        '"https://www.mediafire.com/download_repair.php?qkey=abc&template=59&origin=malware_advisory",'
        '"https://www.mediafire.com/download_repair.php?qkey=abc&template=59&origin=server_error",'
        "{pass: 'bd609'          });</script>"
    )
    assert parse_post_data(page) == {"pass": "bd609"}
    assert parse_post_data("<html>ok</html>") == {}


def test_nombre_desde_html_y_desde_url():
    assert parse_filename('<div class="filename">Mi &amp; Archivo.zip</div>', DIRECT) == "Mi & Archivo.zip"
    assert parse_filename("", DIRECT) == "Mi Archivo.zip"
    assert parse_filename("", "https://download1.mediafire.com/a/b/IDM+Final+%28Kingz+City%29.rar") == (
        "IDM Final (Kingz City).rar"
    )


def test_quickkey():
    assert quickkey("https://www.mediafire.com/file/l3h0s1mttcwipdv/IDM_Final.rar/file") == "l3h0s1mttcwipdv"
    assert quickkey("https://www.mediafire.com/file_premium/abc123/x.zip/file") == "abc123"
    assert quickkey("https://www.mediafire.com/?abc123") == "abc123"
    assert quickkey("https://www.mediafire.com/folder/xyz/Carpeta") is None


def test_detecta_paginas_de_error():
    assert "peligroso" in parse_error("<h2>Dangerous File Blocked</h2>")
    assert "no existe" in parse_error("<h2>Something appears to be missing</h2>")
    assert parse_error("<html>ok</html>") is None


def test_matches():
    assert MediaFireSource.matches("https://www.mediafire.com/file/abc/x.zip/file")
    assert MediaFireSource.matches("https://download1.mediafire.com/a/b/c.zip")
    assert not MediaFireSource.matches("https://evil.com/mediafire.com")
    assert not MediaFireSource.matches("https://notmediafire.com/file/x")
