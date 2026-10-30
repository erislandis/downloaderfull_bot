"""Handlers de Telegram (aiogram 3)."""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx
from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .drive import DriveClient
from .errors import BridgeError
from .jobs import Job, JobManager
from .oauth import OAuthService
from .sources import SOURCES, resolve_source
from .sources.base import RemoteFile, Source
from .utils import human_size

if TYPE_CHECKING:  # pragma: no cover - evita el import circular en tiempo de ejecución
    from .runtime import Runtime

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
MAX_URLS_PER_MESSAGE = 3
PENDING_TTL = 15 * 60

HELP = (
    "👋 Envíame un enlace de <b>{sites}</b> y lo paso a tu Google Drive.\n"
    "Te devolveré un enlace directo para tu bot de descargas.\n\n"
    "/conectar – autoriza Google Drive desde el navegador\n"
    "/desconectar – olvida la autorización de Google Drive\n"
    "/cancelar – cancela tus transferencias\n"
    "/limpiar – borra de Drive todo lo que subió el bot\n"
    "/espacio – espacio libre en Drive"
)


@dataclass
class Pending:
    user_id: int
    source: Source
    files: list[RemoteFile]
    created: float = field(default_factory=time.monotonic)


@dataclass
class Services:
    settings: Settings
    http: httpx.AsyncClient
    drive: DriveClient
    jobs: JobManager
    runtime: "Runtime"
    oauth: OAuthService
    pending: dict[str, Pending] = field(default_factory=dict)


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
            n = await services.drive.cleanup(None)
        except BridgeError as exc:
            await message.answer(f"❌ {escape(str(exc))}")
            return
        await message.answer(f"🧹 Borré {n} archivo(s) de tu Drive.")

    @router.message(Command("conectar"))
    async def connect(message: Message, services: Services) -> None:
        try:
            url = services.oauth.start_url(message.from_user.id)
        except BridgeError as exc:
            await message.answer(f"❌ {escape(str(exc))}")
            return
        href = escape(url, quote=True)
        await message.answer(
            "🔐 <b>Conecta tu Google Drive</b>\n"
            f'<a href="{href}">Abrir la autorización de Google</a>\n\n'
            f"<code>{escape(url)}</code>\n\n"
            "El enlace caduca en 15 minutos. Al terminar, Drive quedará conectado.",
            disable_web_page_preview=True,
        )

    @router.message(Command("desconectar"))
    async def disconnect(message: Message, services: Services) -> None:
        await services.runtime.forget_drive_token(message.from_user.id)
        await message.answer("🔓 Autorización de Google Drive olvidada. Usa /conectar para volver a conectarla.")

    @router.message(Command("espacio"))
    async def space(message: Message, services: Services) -> None:
        if not services.drive.has_auth:
            await message.answer("🔐 Google Drive no está conectado. Usa /conectar.")
            return
        free = await services.drive.free_bytes()
        await message.answer("Espacio libre en Drive: " + (human_size(free) if free is not None else "ilimitado o no disponible"))

    @router.message(F.text)
    async def on_text(message: Message, services: Services) -> None:
        urls = URL_RE.findall(message.text or "")[:MAX_URLS_PER_MESSAGE]
        if not urls:
            await message.answer("Envíame un enlace de " + ", ".join(s.label for s in SOURCES) + ".")
            return
        for url in urls:
            await _handle_url(message, services, url)

    @router.callback_query(F.data.startswith("go:"))
    async def confirm(call: CallbackQuery, services: Services) -> None:
        pending = services.pending.pop(call.data.split(":", 1)[1], None)
        if pending is None or pending.user_id != call.from_user.id:
            await call.answer("Esta selección expiró. Envía el enlace otra vez.", show_alert=True)
            return
        pos = services.jobs.submit(Job(call.message.chat.id, call.from_user.id, pending.source, pending.files))
        await call.message.edit_text(f"📥 {len(pending.files)} archivo(s) en cola (posición {pos}).")
        await call.answer()

    @router.callback_query(F.data.startswith("no:"))
    async def decline(call: CallbackQuery, services: Services) -> None:
        services.pending.pop(call.data.split(":", 1)[1], None)
        await call.message.edit_text("Cancelado.")
        await call.answer()

    dp = Dispatcher(services=services)
    dp.include_router(router)
    return dp


async def _handle_url(message: Message, services: Services, url: str) -> None:
    if not services.drive.has_auth:
        await message.answer("🔐 Google Drive no está conectado. Usa /conectar para autorizarlo desde el navegador.")
        return
    source = resolve_source(url, services.http)
    if source is None:
        await message.answer("Ese sitio no está soportado. Acepto: " + ", ".join(s.label for s in SOURCES) + ".")
        return

    status = await message.answer(f"🔎 Analizando enlace de {source.label}…")
    try:
        files = await source.inspect(url)
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
    if too_big:
        files = [f for f in files if f not in too_big]
        if not files:
            await status.edit_text(f"❌ El archivo pesa {human_size(too_big[0].size)} y el máximo es {human_size(limit)}.")
            return

    total = sum(f.size or 0 for f in files)
    free = await services.drive.free_bytes()
    if free is not None and total > free:
        await status.edit_text(f"❌ No cabe en tu Drive: necesita {human_size(total)} y quedan {human_size(free)}. Usa /limpiar.")
        return

    if len(files) == 1:
        pos = services.jobs.submit(Job(message.chat.id, message.from_user.id, source, files))
        info = f"<b>{escape(files[0].name)}</b> ({human_size(files[0].size)})"
        await status.edit_text(f"📥 {info}\nEn cola (posición {pos}).")
        return

    _purge_pending(services)
    token = uuid.uuid4().hex[:10]
    services.pending[token] = Pending(message.from_user.id, source, files)
    names = "\n".join(f"• {escape(f.name)} ({human_size(f.size)})" for f in files[:8])
    more = f"\n… y {len(files) - 8} más" if len(files) > 8 else ""
    skipped = f"\n⚠️ Omití {len(too_big)} archivo(s) por superar el máximo." if too_big else ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Subir {len(files)} archivos", callback_data=f"go:{token}"),
        InlineKeyboardButton(text="Cancelar", callback_data=f"no:{token}"),
    ]])
    await status.edit_text(
        f"📁 Carpeta con {len(files)} archivos ({human_size(total)}):\n{names}{more}{skipped}",
        reply_markup=keyboard,
    )


def _purge_pending(services: Services) -> None:
    now = time.monotonic()
    for token in [t for t, p in services.pending.items() if now - p.created > PENDING_TTL]:
        del services.pending[token]
