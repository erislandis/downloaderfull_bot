"""Listado temporal de carpetas y textos/botones para elegir archivos con botones.

El navegador guarda en memoria las carpetas abiertas por un TTL corto: Telegram solo
admite 64 bytes por ``callback_data``, así que los botones llevan un token corto y la
lista vive aquí. Las funciones de formato son puras (fáciles de testear) y devuelven
filas de ``(texto, callback_data)`` que `bot.py` convierte en teclado inline.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .sources.base import RemoteFile
from .utils import human_size

TTL_SECONDS = 30 * 60
MAX_ENTRIES = 100
PAGE_SIZE = 6  # filas de archivos por página (deja margen para la navegación)
MENU_LINES = 12  # líneas visibles del listado en el menú principal
CALLBACK_MAX = 64  # límite de Telegram para callback_data
_CB_PREFIX = "fb:"


@dataclass
class FolderView:
    token: str
    chat_id: int
    user_id: int
    url: str
    files: list[RemoteFile]
    created: float = field(default_factory=time.monotonic)


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

    def put(self, chat_id: int, user_id: int, url: str, files: Sequence[RemoteFile]) -> FolderView:
        self._purge()
        view = FolderView(uuid.uuid4().hex[:12], chat_id, user_id, url, list(files),
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


# -- formato puro (texto y botones) ------------------------------------------------------


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


def folder_title(files: Sequence[RemoteFile]) -> str:
    """Nombre de la carpeta: segmento común de las rutas, o genérico."""
    first = files[0].name if files else ""
    if "/" not in first:
        return "Carpeta"
    prefix = first.split("/", 1)[0]
    if all(f.name.startswith(prefix + "/") for f in files):
        return prefix
    return "Carpeta"


def _file_line(index: int, remote: RemoteFile) -> str:
    return f"{index}. {remote.name} ({human_size(remote.size)})"


def menu_view(view: FolderView) -> tuple[str, list[list[tuple[str, str]]]]:
    """Resumen de la carpeta con las opciones: descargar todo, elegir o descartar."""
    total = sum(f.size or 0 for f in view.files)
    title = folder_title(view.files)
    lines = [f"📁 <b>{title}</b> — {len(view.files)} archivo(s) · {human_size(total)}", ""]
    for i, remote in enumerate(view.files[:MENU_LINES], 1):
        lines.append(_file_line(i, remote))
    if len(view.files) > MENU_LINES:
        lines.append(f"…y {len(view.files) - MENU_LINES} más (usa «Elegir archivo»)")
    lines.append("")
    lines.append("Elige una opción:")
    token = view.token
    rows = [
        [(f"📥 Descargar todo ({len(view.files)})", f"{_CB_PREFIX}{token}:all")],
        [("🔽 Elegir un archivo", f"{_CB_PREFIX}{token}:list")],
        [("❌ Descartar", f"{_CB_PREFIX}{token}:x")],
    ]
    return "\n".join(lines), rows


def page_view(view: FolderView, page: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """Página de archivos individuales (``page`` empieza en 0)."""
    count = max(1, -(-len(view.files) // PAGE_SIZE))  # techo
    page = min(max(page, 0), count - 1)
    token = view.token
    lines = [f"🔽 <b>{folder_title(view.files)}</b> — elige un archivo "
             f"(página {page + 1}/{count}):", ""]
    rows: list[list[tuple[str, str]]] = []
    start = page * PAGE_SIZE
    for i, remote in enumerate(view.files[start:start + PAGE_SIZE], start + 1):
        lines.append(_file_line(i, remote))
        rows.append([(clip(_file_line(i, remote)), f"{_CB_PREFIX}{token}:f{i - 1}")])
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("⬅️", f"{_CB_PREFIX}{token}:p{page - 1}"))
    if page + 1 < count:
        nav.append(("➡️", f"{_CB_PREFIX}{token}:p{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("📥 Descargar todo", f"{_CB_PREFIX}{token}:all"),
                 ("⬅️ Menú", f"{_CB_PREFIX}{token}:m"),
                 ("❌", f"{_CB_PREFIX}{token}:x")])
    return "\n".join(lines), rows
