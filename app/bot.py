"""Handlers de Telegram (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html import escape
from typing import Any, Awaitable, Callable

import httpx
from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .errors import BridgeError, TransientError
from .folders import FolderBrowser, FolderView, menu_view, page_view, parse_cb
from .jobs import Job, JobManager
from .sources import SOURCES, resolve_source
from .sources.base import RemoteFile, Source
from .uploader import TelegramUploader
from .utils import human_size

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
MAX_URLS_PER_MESSAGE = 3
INSPECT_RETRIES = 3  # reintentos ante fallos transitorios del sitio (5xx, rate-limit)
INSPECT_BASE_DELAY = 1.5

HELP = (
    "👋 Envíame un enlace de <b>{sites}</b> (archivo suelto o carpeta de Mega)\n"
    "y subiré el archivo a Telegram.\n"
    "Al terminar lo tendrás aquí mismo como documento.\n\n"
    "/cancelar – cancela tus transferencias\n"
    "/limpiar – borra los archivos almacenados por el bot"
)


@dataclass
class Services:
    settings: Settings
    http: httpx.AsyncClient
    uploader: TelegramUploader
    jobs: JobManager
    folders: FolderBrowser


class AuthMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]) -> None:
        self._allowed = allowed

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user and user.id in self._allowed:
            return await handler(event, data)
        if isinstance(event, Message):
            uid = user.id if user else "desconocido"
            await event.answer(f"🔒 No tienes acceso a este bot.\nTu ID de Telegram es <code>{uid}</code>.")
        elif isinstance(event, CallbackQuery):
            await event.answer("No autorizado", show_alert=True)
        return None


def build_dispatcher(services: Services) -> Dispatcher:
    router = Router()
    auth = AuthMiddleware(services.settings.allowed_user_ids)
    router.message.outer_middleware(auth)
    router.callback_query.outer_middleware(auth)

    @router.message(CommandStart())
    @router.message(Command("ayuda"))
    async def start(message: Message) -> None:
        await message.answer(HELP.format(sites=", ".join(s.label for s in SOURCES)))

    @router.message(Command("cancelar"))
    async def cancel(message: Message, services: Services) -> None:
        n = services.jobs.cancel_user(message.from_user.id)
        await message.answer(f"🛑 Cancelé {n} trabajo(s)." if n else "No tienes trabajos activos.")

    @router.message(Command("limpiar"))
    async def clean(message: Message, services: Services) -> None:
        try:
            n = await services.uploader.cleanup(None)
        except BridgeError as exc:
            await message.answer(f"❌ {escape(str(exc))}")
            return
        await message.answer(f"🧹 Borré {n} archivo(s) almacenados." if n else "No había nada que borrar.")

    @router.message(F.text)
    async def on_text(message: Message, services: Services) -> None:
        urls = URL_RE.findall(message.text or "")[:MAX_URLS_PER_MESSAGE]
        if not urls:
            await message.answer("Envíame un enlace de " + ", ".join(s.label for s in SOURCES) + ".")
            return
        for url in urls:
            await _handle_url(message, services, url)

    @router.callback_query(F.data.startswith("fb:"))
    async def on_folder_action(callback: CallbackQuery, services: Services) -> None:
        await _handle_folder_action(callback, services)

    dp = Dispatcher(services=services)
    dp.include_router(router)
    return dp


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


async def _safe_edit_msg(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        pass  # mensaje ya editado/borrado: el listado caducado se gestiona aparte


async def _inspect_with_retry(source: Source, url: str) -> list[RemoteFile]:
    """Reintenta el análisis ante fallos transitorios (MediaFire/Mega respondiendo mal)."""
    for attempt in range(INSPECT_RETRIES):
        try:
            return await source.inspect(url)
        except TransientError:
            if attempt + 1 >= INSPECT_RETRIES:
                raise
            await asyncio.sleep(INSPECT_BASE_DELAY * (attempt + 1))
    raise AssertionError("inalcanzable")


async def _handle_url(message: Message, services: Services, url: str) -> None:
    source = resolve_source(url, services.http)
    if source is None:
        await message.answer("Ese sitio no está soportado. Acepto: " + ", ".join(s.label for s in SOURCES) + ".")
        return

    status = await message.answer(f"🔎 Analizando enlace de {source.label}…")
    try:
        files = await _inspect_with_retry(source, url)
    except BridgeError as exc:
        await status.edit_text(f"❌ {escape(str(exc))}")
        return
    except httpx.HTTPError:
        await status.edit_text("❌ No pude contactar con el sitio. Inténtalo de nuevo.")
        return
    except Exception:  # noqa: BLE001
        log.exception("Error inesperado analizando %s", url)
        await status.edit_text("❌ Error inesperado al analizar el enlace.")
        return

    limit = services.settings.max_file_bytes
    too_big = [f for f in files if f.size and f.size > limit]
    if too_big and len(files) == 1:
        await status.edit_text(
            f"❌ El archivo pesa {human_size(too_big[0].size)} y el máximo es {human_size(limit)}."
        )
        return

    if len(files) == 1:
        remote = files[0]
        pos = services.jobs.submit(Job(message.chat.id, message.from_user.id, source, files))
        await status.edit_text(f"📥 <b>{escape(remote.name)}</b> ({human_size(remote.size)})\nEn cola (posición {pos}).")
        return

    # Carpeta: se listan los archivos y se pide qué descargar (todo o uno suelto).
    if too_big and len(too_big) == len(files):
        await status.edit_text(
            f"❌ Los {len(files)} archivos superan el máximo de {human_size(limit)} cada uno."
        )
        return
    view = services.folders.put(message.chat.id, message.from_user.id, url, files)
    text, rows = menu_view(view)
    if too_big:
        text += (f"\n⚠️ {len(too_big)} archivo(s) superan {human_size(limit)} y se omitirán "
                 "al elegir «Descargar todo».")
    await _safe_edit_msg(status, text, _kb(rows))


async def _handle_folder_action(callback: CallbackQuery, services: Services) -> None:
    parsed = parse_cb(callback.data)
    if parsed is None:
        await callback.answer()
        return
    token, action = parsed
    view = services.folders.get(token)
    if view is None:
        await callback.answer("El listado caducó; envía el enlace otra vez.", show_alert=True)
        return

    msg = callback.message
    if action == "x":
        services.folders.drop(token)
        await callback.answer("Listado descartado.")
        await _safe_edit_msg(msg, "❌ Listado descartado.")
        return
    if action == "m":
        text, rows = menu_view(view)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action.startswith("p"):
        try:
            page = int(action[1:] or 0)
        except ValueError:
            await callback.answer()
            return
        text, rows = page_view(view, page)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action == "list":
        text, rows = page_view(view, 0)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return

    limit = services.settings.max_file_bytes
    if action == "all":
        selected = [f for f in view.files if not (f.size and f.size > limit)]
        skipped = len(view.files) - len(selected)
        if not selected:
            await callback.answer(f"Ningún archivo cabe en {human_size(limit)}.", show_alert=True)
            return
    elif action.startswith("f"):
        try:
            index = int(action[1:])
            remote = view.files[index]
        except (ValueError, IndexError):
            await callback.answer("Ese archivo ya no está en el listado.", show_alert=True)
            return
        if remote.size and remote.size > limit:
            await callback.answer(
                f"Pesa {human_size(remote.size)}; el máximo es {human_size(limit)}.", show_alert=True
            )
            return
        selected, skipped = [remote], 0
    else:
        await callback.answer()
        return

    source = resolve_source(view.url, services.http)
    if source is None:
        await callback.answer("No pude volver a resolver el enlace.", show_alert=True)
        return
    services.folders.drop(token)
    pos = services.jobs.submit(Job(view.chat_id, view.user_id, source, selected))
    total = len(selected)
    note = ""
    if skipped:
        note = f"\n⚠️ Omití {skipped} archivo(s) por tamaño (máx. {human_size(limit)})."
    label = f"<b>{escape(selected[0].name)}</b>" if total == 1 else f"{total} archivo(s)"
    await callback.answer("En cola ✓" if total == 1 else f"{total} archivos en cola ✓")
    await _safe_edit_msg(msg, f"📥 {label}\nEn cola (posición {pos}).{note}")
