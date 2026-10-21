"""Navegador de carpetas (árbol) y textos/botones para elegir qué descargar.

El navegador guarda en memoria las carpetas abiertas por un TTL corto: Telegram solo
admite 64 bytes por ``callback_data``, así que los botones llevan un token corto y el
árbol vive aquí. Las funciones de formato son puras (fáciles de testear) y devuelven
filas de ``(texto, callback_data)`` que `bot.py` convierte en teclado inline.

Acciones (callback ``fb:<token>:<acción>``):

* ``s<índice>``  entrar en la subcarpeta ``<índice>``
* ``f<índice>``  mostrar las opciones del archivo ``<índice>``
* ``fd<índice>`` descargar ese archivo
* ``fz<índice>`` comprimir ese archivo en un ZIP
* ``p<n>``       ir a la página ``n`` del listado
* ``z``          comprimir la carpeta actual en un único ZIP
* ``u``          subir un nivel
* ``x``          descartar el listado
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from html import escape
from typing import Callable

from .sources.base import BundleFile, FolderNode, RemoteFile, build_bundle
from .utils import human_size, safe_filename

TTL_SECONDS = 30 * 60
MAX_ENTRIES = 100
PAGE_SIZE = 6  # elementos por página (deja margen para la navegación)
CALLBACK_MAX = 64  # límite de Telegram para callback_data
_CB_PREFIX = "fb:"


@dataclass
class FolderView:
    token: str
    chat_id: int
    user_id: int
    url: str
    root: FolderNode  # raíz del árbol abierto
    current: FolderNode  # carpeta que se está viendo
    page: int = 0
    created: float = field(default_factory=time.monotonic)

    @property
    def is_root(self) -> bool:
        return self.current.node_id == self.root.node_id


class FolderBrowser:
    """Memoria de carpetas abiertas con TTL y aforo máximo."""

    def __init__(self, ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._max, self._clock = ttl, max_entries, clock
        self._items: dict[str, FolderView] = {}

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, v in self._items.items() if now - v.created > self._ttl]:
            del self._items[token]

    def put(self, chat_id: int, user_id: int, url: str, root: FolderNode) -> FolderView:
        self._purge()
        view = FolderView(uuid.uuid4().hex[:12], chat_id, user_id, url, root, root,
                          created=self._clock())
        self._items[view.token] = view
        while len(self._items) > self._max:
            oldest = min(self._items, key=lambda t: self._items[t].created)
            del self._items[oldest]
        return view

    def get(self, token: str) -> FolderView | None:
        self._purge()
        return self._items.get(token)

    def drop(self, token: str) -> None:
        self._items.pop(token, None)

    def __len__(self) -> int:
        return len(self._items)


# -- callbacks ---------------------------------------------------------------------------


def parse_cb(data: str | None) -> tuple[str, str] | None:
    """``fb:<token>:<acción>`` → ``(token, acción)``; ``None`` si no corresponde."""
    if not data or not data.startswith(_CB_PREFIX):
        return None
    token, sep, action = data[len(_CB_PREFIX):].partition(":")
    if not sep or not token or not action:
        return None
    return token, action


def clip(text: str, limit: int = CALLBACK_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def zip_name(node: FolderNode) -> str:
    """Nombre del ZIP que se genera con una carpeta."""
    return safe_filename(node.name) + ".zip"


# -- formato puro (texto y botones) ------------------------------------------------------


def _child_label(child: FolderNode) -> str:
    if child.is_dir:
        return f"📂 {child.name}"
    return f"📄 {child.name} ({human_size(child.size)})"


def _summary(node: FolderNode) -> str:
    dirs = sum(1 for c in node.children if c.is_dir)
    files = sum(1 for c in node.children if not c.is_dir)
    return f"{dirs} subcarpeta(s) · {files} archivo(s) · {human_size(node.size)}"


def menu_view(view: FolderView) -> tuple[str, list[list[tuple[str, str]]]]:
    """Vista de la carpeta actual: subcarpetas y archivos, con las acciones."""
    node = view.current
    header: list[str] = []
    if node.path and node.path != node.name:
        header.append(f"📂 {escape(node.path)}")
    header.append(f"📁 <b>{escape(node.name)}</b>")
    header.append(_summary(node))
    body, rows = page_view(view, view.page)
    if view.is_root:
        hint = "Pulsa 🗜 para que te envíe toda la carpeta en un ZIP, o elige un archivo:"
    else:
        hint = "Pulsa ⬆️ para subir un nivel, 🗜 para comprimir esta carpeta, o elige un archivo:"
    lines = header + ["", hint]
    if body:
        lines.append(body)
    lines.append("")
    lines.append("Puedes cerrar Telegram: el listado sigue activo 30 minutos y te lo dejo aquí.")
    return "\n".join(lines), rows


def page_view(view: FolderView, page: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Página con el contenido de la carpeta actual (``page`` empieza en 0)."""
    node = view.current
    children = node.children
    count = max(1, -(-len(children) // PAGE_SIZE))  # techo
    page = min(max(page, 0), count - 1)
    token = view.token
    lines: list[str] = []
    rows: list[list[tuple[str, str]]] = []
    start = page * PAGE_SIZE
    for index, child in enumerate(children[start:start + PAGE_SIZE], start):
        label = _child_label(child)
        lines.append(f"{index + 1}. {escape(label)}")
        action = "s" if child.is_dir else "f"
        rows.append([(clip(label), f"{_CB_PREFIX}{token}:{action}{index}")])
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("⬅️", f"{_CB_PREFIX}{token}:p{page - 1}"))
    if page + 1 < count:
        nav.append(("➡️", f"{_CB_PREFIX}{token}:p{page + 1}"))
    if nav:
        rows.append(nav)
    actions = [("🗜 Comprimir en ZIP", f"{_CB_PREFIX}{token}:z")]
    if not view.is_root:
        actions.append(("⬆️ Subir", f"{_CB_PREFIX}{token}:u"))
    actions.append(("❌ Descartar", f"{_CB_PREFIX}{token}:x"))
    rows.append([actions[0]])
    if len(actions) > 1:
        rows.append(actions[1:])
    return "\n".join(lines), rows


def file_view(view: FolderView, index: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Opciones de un archivo concreto: descargarlo o comprimirlo en un ZIP."""
    child = view.current.children[index]
    token = view.token
    text = (
        f"📄 <b>{escape(child.name)}</b>\n"
        f"{human_size(child.size)}\n\n"
        "¿Qué hago con este archivo?"
    )
    rows = [
        [("⬇️ Descargar", f"{_CB_PREFIX}{token}:fd{index}")],
        [("🗜 Comprimir en ZIP", f"{_CB_PREFIX}{token}:fz{index}")],
        [("⬅️ Volver", f"{_CB_PREFIX}{token}:m")],
    ]
    return text, rows


def bundle_for(view: FolderView) -> BundleFile:
    """ZIP con todo el contenido de la carpeta actual."""
    node = view.current
    return build_bundle(node, zip_name(node))


def bundle_for_file(node: FolderNode) -> BundleFile:
    """ZIP con un único archivo (el que el usuario ha elegido)."""
    member = RemoteFile(node.name, node.size, node.ref, path=node.name)
    return BundleFile(
        name=safe_filename(node.name) + ".zip",
        size=node.size or 0,
        ref=f"bundle:{node.node_id}",
        members=(member,),
    )
