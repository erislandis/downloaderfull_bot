import pytest

from app.folders import (
    CALLBACK_MAX,
    PAGE_SIZE,
    FolderBrowser,
    bundle_for,
    bundle_for_file,
    file_view,
    menu_view,
    page_view,
    parse_cb,
    zip_name,
)
from app.sources.base import FolderNode, RemoteFile


def node(name: str, is_dir: bool = False, size: int = 100, path: str = "") -> FolderNode:
    return FolderNode(
        name=name,
        node_id=f"n-{name}",
        size=size,
        is_dir=is_dir,
        ref="" if is_dir else f"ref-{name}",
        path=path or name,
    )


def tree(*children: FolderNode) -> FolderNode:
    root = FolderNode(name="mi carpeta", node_id="root", is_dir=True)
    root.children = list(children)
    root.size = sum(c.size or 0 for c in children)
    return root


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
    view = browser.put(chat_id=1, user_id=2, url="https://mega.nz/folder/x", root=tree(node("a.bin")))
    assert len(browser) == 1
    got = browser.get(view.token)
    assert got is view and got.chat_id == 1 and got.user_id == 2
    browser.drop(view.token)
    assert browser.get(view.token) is None and len(browser) == 0


def test_caduca_por_ttl():
    clock = FakeClock()
    browser = FolderBrowser(ttl=100, clock=clock)
    view = browser.put(1, 2, "u", tree(node("a.bin")))
    clock.now = 99
    assert browser.get(view.token) is view
    clock.now = 101
    assert browser.get(view.token) is None


def test_aforo_maximo_expulsa_el_mas_viejo():
    clock = FakeClock()
    browser = FolderBrowser(max_entries=2, clock=clock)
    first = browser.put(1, 2, "u", tree(node("a.bin")))
    clock.now = 1
    second = browser.put(1, 2, "u", tree(node("b.bin")))
    clock.now = 2
    browser.put(1, 2, "u", tree(node("c.bin")))
    assert browser.get(first.token) is None
    assert browser.get(second.token) is second
    assert len(browser) == 2


# -- callbacks ---------------------------------------------------------------------------


def test_parse_cb():
    assert parse_cb("fb:abc123:all") == ("abc123", "all")
    assert parse_cb("fb:abc123:f5") == ("abc123", "f5")
    assert parse_cb("fb:abc123:fd5") == ("abc123", "fd5")
    assert parse_cb("fb:abc123:fz5") == ("abc123", "fz5")
    assert parse_cb("fb:abc123:s2") == ("abc123", "s2")
    assert parse_cb("fb:abc123:p2") == ("abc123", "p2")
    assert parse_cb("fb:abc123:z") == ("abc123", "z")
    assert parse_cb("fb:abc123:u") == ("abc123", "u")
    for bad in (None, "", "otro:x", "fb::all", "fb:tok:", "fb:tok"):
        assert parse_cb(bad) is None


def test_callback_data_no_supera_el_limite_de_telegram():
    view = FolderBrowser().put(1, 2, "u", tree(node("a.bin")))
    _, rows = menu_view(view)
    for row in rows:
        for _, data in row:
            assert len(data) <= CALLBACK_MAX


# -- textos y botones --------------------------------------------------------------------


def test_menu_muestra_la_carpeta_y_sus_botones():
    root = tree(node("sub", is_dir=True, size=0), node("f1.bin", size=1024))
    view = FolderBrowser().put(1, 2, "u", root)
    text, rows = menu_view(view)
    assert "📁 <b>mi carpeta</b>" in text
    assert "1 subcarpeta(s) · 1 archivo(s)" in text
    labels = [t for row in rows for t, _ in row]
    assert any("Comprimir en ZIP" in t for t in labels)
    assert any("Subir" not in t for t in labels)  # en la raíz no se puede subir
    datas = [d for row in rows for _, d in row]
    assert any(d.endswith(":z") for d in datas)
    assert any(d.endswith(":s0") for d in datas)
    assert any(d.endswith(":f1") for d in datas)


def test_menu_de_subcarpeta_ofrece_subir():
    root = tree(node("sub", is_dir=True, size=0))
    view = FolderBrowser().put(1, 2, "u", root)
    view.current = root.children[0]
    _, rows = menu_view(view)
    labels = [t for row in rows for t, _ in row]
    assert any("Subir" in t for t in labels)


def test_paginas_y_navegacion():
    root = tree(*[node(f"f{i}.bin") for i in range(10)])
    view = FolderBrowser().put(1, 2, "u", root)
    text, rows = page_view(view, 0)
    assert "1. 📄 f0.bin (100 B)" in text and "6. 📄 f5.bin (100 B)" in text
    labels = [t for row in rows for t, _ in row]
    assert "➡️" in labels and "⬅️" not in labels  # solo hay adelante en la 1ª página
    text, rows = page_view(view, 99)  # fuera de rango → última página
    assert "10. 📄 f9.bin (100 B)" in text
    labels = [t for row in rows for t, _ in row]
    assert "⬅️" in labels and "➡️" not in labels


def test_opciones_de_un_archivo():
    root = tree(node("f1.bin", size=2048))
    view = FolderBrowser().put(1, 2, "u", root)
    text, rows = file_view(view, 0)
    assert "f1.bin" in text and "2.0 KB" in text
    datas = [d for row in rows for _, d in row]
    assert any(d.endswith(":fd0") for d in datas)
    assert any(d.endswith(":fz0") for d in datas)


def test_boton_de_archivo_por_indice():
    root = tree(node("a.bin"), node("b.bin"))
    view = FolderBrowser().put(1, 2, "u", root)
    _, rows = page_view(view, 0)
    assert rows[0][0][1] == f"fb:{view.token}:f0"
    assert parse_cb(rows[0][0][1]) == (view.token, "f0")


# -- ZIP ---------------------------------------------------------------------------------


def test_zip_de_la_carpeta_actual():
    sub = node("sub", is_dir=True, size=0)
    inner = node("dentro.bin", size=10, path="sub/dentro.bin")
    sub.children = [inner]
    root = tree(sub, node("suelto.bin", size=20))
    view = FolderBrowser().put(1, 2, "u", root)
    bundle = bundle_for(view)
    assert bundle.name == "mi carpeta.zip"
    assert [m.path for m in bundle.members] == ["mi carpeta/sub/dentro.bin", "mi carpeta/suelto.bin"]
    assert bundle.size == 30


def test_zip_de_un_solo_archivo():
    root = tree(node("suelto.bin", size=20))
    view = FolderBrowser().put(1, 2, "u", root)
    bundle = bundle_for_file(root.children[0])
    assert bundle.name == "suelto.bin.zip"
    assert len(bundle.members) == 1
    assert bundle.members[0].path == "suelto.bin"
    assert bundle.size == 20


def test_nombre_del_zip_saneado():
    assert zip_name(node("carpeta/rma?*:")) == "carpeta_rma___.zip"
