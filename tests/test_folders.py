import pytest

from app.folders import (
    CALLBACK_MAX, PAGE_SIZE, FolderBrowser, folder_title, menu_view, page_view, parse_cb,
)
from app.sources.base import RemoteFile


def files(*names: str, size: int = 100) -> list[RemoteFile]:
    return [RemoteFile(n, size, f"ref-{i}") for i, n in enumerate(names)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# -- navegador ---------------------------------------------------------------------------


def test_put_get_drop():
    browser = FolderBrowser(clock=FakeClock())
    view = browser.put(chat_id=1, user_id=2, url="https://mega.nz/folder/x", files=files("a.bin"))
    assert len(browser) == 1
    got = browser.get(view.token)
    assert got is view and got.chat_id == 1 and got.user_id == 2
    browser.drop(view.token)
    assert browser.get(view.token) is None and len(browser) == 0


def test_caduca_por_ttl():
    clock = FakeClock()
    browser = FolderBrowser(ttl=100, clock=clock)
    view = browser.put(1, 2, "u", files("a.bin"))
    clock.now = 99
    assert browser.get(view.token) is view
    clock.now = 101
    assert browser.get(view.token) is None


def test_aforo_maximo_expulsa_el_mas_viejo():
    clock = FakeClock()
    browser = FolderBrowser(max_entries=2, clock=clock)
    first = browser.put(1, 2, "u", files("a.bin"))
    clock.now = 1
    second = browser.put(1, 2, "u", files("b.bin"))
    clock.now = 2
    browser.put(1, 2, "u", files("c.bin"))
    assert browser.get(first.token) is None
    assert browser.get(second.token) is second
    assert len(browser) == 2


# -- callbacks ---------------------------------------------------------------------------


def test_parse_cb():
    assert parse_cb("fb:abc123:all") == ("abc123", "all")
    assert parse_cb("fb:abc123:f5") == ("abc123", "f5")
    assert parse_cb("fb:abc123:p2") == ("abc123", "p2")
    for bad in (None, "", "otro:x", "fb::all", "fb:tok:", "fb:tok"):
        assert parse_cb(bad) is None


def test_callback_data_no_supera_el_limite_de_telegram():
    view = FolderBrowser().put(1, 2, "u", files("a.bin"))
    _, rows = menu_view(view)
    for row in rows:
        for _, data in row:
            assert len(data) <= CALLBACK_MAX


# -- textos y botones --------------------------------------------------------------------


def test_folder_title():
    assert folder_title(files("trabajos/nota.txt", "trabajos/otra.txt")) == "trabajos"
    assert folder_title(files("a.txt", "b.txt")) == "Carpeta"
    assert folder_title([]) == "Carpeta"


def test_menu_lista_archivos_y_opciones():
    view = FolderBrowser().put(1, 2, "u", files("carpeta/f1.bin", "carpeta/f2.bin", size=1024))
    text, rows = menu_view(view)
    assert "📁 <b>carpeta</b> — 2 archivo(s) · 2.0 KB" in text
    assert "1. carpeta/f1.bin (1.0 KB)" in text
    assert "2. carpeta/f2.bin (1.0 KB)" in text
    labels = [t for row in rows for t, _ in row]
    assert any("Descargar todo" in t for t in labels)
    assert any("Elegir un archivo" in t for t in labels)
    assert rows[0][0][1].endswith(":all")


def test_menu_recorta_listado_largo():
    many = files(*[f"carpeta/f{i}.bin" for i in range(20)])
    view = FolderBrowser().put(1, 2, "u", many)
    text, _ = menu_view(view)
    assert "…y 8 más" in text


def test_paginas_y_navegacion():
    many = files(*[f"c/f{i}.bin" for i in range(10)])
    view = FolderBrowser().put(1, 2, "u", many)
    text, rows = page_view(view, 0)
    assert f"página 1/{-(-10 // PAGE_SIZE)}" in text
    labels = [t for row in rows for t, _ in row]
    assert "1. c/f0.bin (100 B)" in text and "6. c/f5.bin (100 B)" in text
    assert "➡️" in labels and "⬅️" not in labels  # solo hay adelante en la 1ª página
    text, rows = page_view(view, 99)  # fuera de rango → última página
    assert "10. c/f9.bin (100 B)" in text
    labels = [t for row in rows for t, _ in row]
    assert "⬅️" in labels and "➡️" not in labels
    datas = [d for row in rows for _, d in row]
    assert any(d.endswith(":all") for d in datas)
    assert any(d.endswith(":m") for d in datas)
    assert any(d.endswith(":x") for d in datas)


def test_boton_archivo_por_indice():
    view = FolderBrowser().put(1, 2, "u", files("a.bin", "b.bin"))
    _, rows = page_view(view, 0)
    first_row_data = rows[0][0][1]
    assert first_row_data == f"fb:{view.token}:f0"
    assert parse_cb(first_row_data) == (view.token, "f0")
