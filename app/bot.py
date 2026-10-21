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

from .admin import (
    AdminRegistry,
    format_admin_panel,
    format_queue,
    format_users,
    is_admin_command,
    parse_user_id,
)
from .config import Settings
from .errors import BridgeError, TransientError
from .folders import FolderBrowser, FolderView, bundle_for, bundle_for_file, file_view, menu_view, parse_cb
from .jobs import MAX_JOBS_PER_USER, Job, JobManager
from .sources import SOURCES, resolve_source
from .sources.base import (
    BundleFile,
    FolderNode,
    Inspection,
    RemoteFile,
    Source,
    collect_files,
    node_to_file,
    parent_of,
    tree_from_files,
)
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
    "En las carpetas de Mega puedes navegar, elegir un archivo o comprimir\n"
    "la carpeta (o un archivo) en un único ZIP.\n\n"
    "Puedes cerrar Telegram cuando quieras: la descarga y la subida siguen en el\n"
    "servidor y el archivo te espera en este chat al volver.\n\n"
    "/estado – qué transferencias tienes en marcha o entregadas\n"
    "/cancelar – cancela tus transferencias"
)


@dataclass
class Services:
    settings: Settings
    http: httpx.AsyncClient
    uploader: TelegramUploader
    jobs: JobManager
    folders: FolderBrowser
    admin: AdminRegistry
    status: Callable[[], dict[str, Any]]  # instantánea del runtime (uptime, bot, MTProto)


class AdminMiddleware(BaseMiddleware):
    """Deja pasar los mensajes normales y bloquea los comandos de administración.

    Solo se bloquea si el mensaje **es** un comando de administración y quien lo manda no
    es administrador: el resto de mensajes (enlaces, /estado, texto) siguen su curso.
    """

    def __init__(self, admins: AdminRegistry) -> None:
        self._admins = admins

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        if not is_admin_command(getattr(event, "text", None)):
            return await handler(event, data)
        user = data.get("event_from_user")
        if self._admins.is_admin(getattr(user, "id", None)):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("🔒 Este comando es solo para el administrador del bot.")
        return None


def build_dispatcher(services: Services) -> Dispatcher:
    """Monta los routers.

    El bot es **público**: cualquiera puede enviar enlaces y usar el navegador de carpetas.
    Los comandos de administración viven en un router aparte, protegido por un middleware
    que exige ser administrador. El router de administración se incluye **primero** para
    que sus comandos no acaben en el manejador de texto libre.
    """
    admin_router = Router(name="admin")
    admin_router.message.outer_middleware(AdminMiddleware(services.admin))

    @admin_router.message(Command("admin", ignore_case=True))
    async def admin_panel(message: Message, services: Services) -> None:
        """Estado general del bot: cola, MTProto, usuarios y límites."""
        st = services.status()
        queue = st.get("queue") or {}
        mtproto = st.get("mtproto") or {}
        await message.answer(
            format_admin_panel(
                bot=st.get("bot"),
                uptime_seconds=int(st.get("uptime_seconds") or 0),
                queue_pending=int(queue.get("pending_jobs") or 0),
                queue_current=queue.get("current_file"),
                mtproto_ok=bool(mtproto.get("ok")),
                mtproto_error=str(mtproto.get("error") or ""),
                users=len(services.jobs.known_users()),
                admins=len(services.admin),
                max_file_bytes=services.settings.max_file_bytes,
                sources=[s.label for s in SOURCES],
            )
        )

    @admin_router.message(Command("cola", ignore_case=True))
    async def admin_queue(message: Message, services: Services) -> None:
        await message.answer(format_queue(services.jobs.queue_items()))

    @admin_router.message(Command("usuarios", ignore_case=True))
    async def admin_users(message: Message, services: Services) -> None:
        await message.answer(format_users(services.jobs.user_stats()))

    @admin_router.message(Command("cancelar_todo", ignore_case=True))
    async def admin_cancel_all(message: Message, services: Services) -> None:
        n = services.jobs.cancel_all()
        await message.answer(f"🛑 Cancelé {n} trabajo(s)." if n else "No hay transferencias en curso.")

    @admin_router.message(Command("limpiar", ignore_case=True))
    async def admin_clean(message: Message, services: Services) -> None:
        """Borra todo lo que el bot tiene almacenado en Telegram (afecta a todos)."""
        try:
            n = await services.uploader.cleanup(None)
        except BridgeError as exc:
            await message.answer(f"❌ {escape(str(exc))}")
            return
        await message.answer(f"🧹 Borré {n} archivo(s) almacenados." if n else "No había nada que borrar.")

    @admin_router.message(Command("broadcast", ignore_case=True), F.text)
    async def admin_broadcast(message: Message, services: Services) -> None:
        parts = (message.text or "").split(maxsplit=1)
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            await message.answer("Uso: <code>/broadcast mensaje</code>")
            return
        sent, failed = await services.jobs.broadcast(body)
        extra = f" ({failed} fallaron)" if failed else ""
        await message.answer(f"📣 Aviso enviado a {sent} usuario(s){extra}.")

    @admin_router.message(Command("permitir", ignore_case=True))
    async def admin_allow(message: Message, services: Services) -> None:
        user_id = parse_user_id(message.text)
        if user_id is None:
            await message.answer("Uso: <code>/permitir id_de_telegram</code>")
            return
        if services.admin.grant(user_id):
            await message.answer(f"✅ <code>{user_id}</code> ya puede usar los comandos de administración.")
        else:
            await message.answer(f"ℹ️ <code>{user_id}</code> ya era administrador.")

    @admin_router.message(Command("revocar", ignore_case=True))
    async def admin_revoke(message: Message, services: Services) -> None:
        user_id = parse_user_id(message.text)
        if user_id is None:
            await message.answer("Uso: <code>/revocar id_de_telegram</code>")
            return
        if not services.admin.revoke(user_id):
            await message.answer(f"ℹ️ <code>{user_id}</code> no era administrador.")
            return
        if user_id == message.from_user.id:
            await message.answer(f"🚫 Te has quitado a ti mismo los permisos de administración.")
        else:
            await message.answer(f"🚫 <code>{user_id}</code> ya no puede usar los comandos de administración.")

    router = Router(name="user")

    @router.message(CommandStart())
    @router.message(Command("ayuda", ignore_case=True))
    async def start(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        await message.answer(
            HELP.format(sites=", ".join(s.label for s in SOURCES))
            + f"\n\nTu ID de Telegram es <code>{uid}</code>."
        )

    @router.message(Command("cancelar", ignore_case=True))
    async def cancel(message: Message, services: Services) -> None:
        n = services.jobs.cancel_user(message.from_user.id)
        await message.answer(f"🛑 Cancelé {n} trabajo(s)." if n else "No tienes trabajos activos.")

    @router.message(Command("estado", ignore_case=True))
    async def estado(message: Message, services: Services) -> None:
        """Estado de tus transferencias: útil al volver al chat tras estar desconectado."""
        items = services.jobs.user_status(message.from_user.id)
        if not items:
            await message.answer(
                "No tengo transferencias tuyas en cola ni entregadas recientemente.\n"
                "Envíame un enlace de MediaFire o Mega y lo subo aquí."
            )
            return
        lines = ["<b>📋 Tus transferencias</b>"]
        for item in items:
            name = escape(str(item.get("name") or "?"))
            size = item.get("size")
            state = str(item.get("state") or "")
            if state == "queued":
                mark = "⏳ en cola"
            elif state == "running":
                mark = "🔄 subiendo"
            elif state == "done":
                mark = "✅ entregado (ya está en este chat)"
            else:
                mark = "❌ falló"
            extra = f" — {escape(str(item['note']))}" if item.get("note") else ""
            weight = f" ({human_size(int(size))})" if isinstance(size, int) else ""
            lines.append(f"· {name}{weight}: {mark}{extra}")
        lines.append("\nPuedes cerrar Telegram: la transferencia sigue en el servidor.")
        await message.answer("\n".join(lines))

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
    dp.include_router(admin_router)
    dp.include_router(router)
    return dp


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def _submit(
    services: Services, chat_id: int, user_id: int, source: Source, remote: RemoteFile
) -> int | None:
    """Encola un trabajo si el usuario no ha superado el límite de transferencias.

    El bot es público: sin este tope, un usuario podría llenar la cola y agotar la cuota
    de descarga del resto. Devuelve la posición en la cola o None si no se encoló.
    """
    if services.jobs.pending_for(user_id) >= MAX_JOBS_PER_USER:
        return None
    return services.jobs.submit(Job(chat_id, user_id, source, [remote]))


QUEUE_FULL = (
    f"⏳ Ya tienes {MAX_JOBS_PER_USER} transferencias en cola. Espera a que terminen "
    "antes de enviar más enlaces."
)


async def _safe_edit_msg(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        pass  # mensaje ya editado/borrado: el listado caducado se gestiona aparte


async def _inspect_with_retry(source: Source, url: str) -> Inspection:
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
        inspection = await _inspect_with_retry(source, url)
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

    # Carpeta navegable (Mega): se ofrece el árbol para elegir qué enviar.
    if inspection.tree is not None:
        tree = inspection.tree
        if not collect_files(tree):
            await status.edit_text("❌ Esa carpeta de Mega está vacía.")
            return
        view = services.folders.put(message.chat.id, message.from_user.id, url, tree)
        text, rows = menu_view(view)
        await _safe_edit_msg(status, text, _kb(rows))
        return

    files = inspection.files
    if not files:
        await status.edit_text("❌ No encontré ningún archivo en ese enlace.")
        return

    too_big = [f for f in files if f.size and f.size > limit]
    if too_big and len(files) == 1:
        await status.edit_text(
            f"❌ El archivo pesa {human_size(too_big[0].size)} y el máximo es {human_size(limit)}."
        )
        return

    if len(files) == 1:
        remote = files[0]
        pos = _submit(services, message.chat.id, message.from_user.id, source, remote)
        if pos is None:
            await status.edit_text(QUEUE_FULL)
            return
        await status.edit_text(
            f"📥 <b>{escape(remote.name)}</b> ({human_size(remote.size)})\n"
            f"En cola (posición {pos}). Puedes cerrar Telegram: te lo dejo aquí al terminar.\n"
            "Consulta <code>/estado</code> cuando quieras."
        )
        return

    # Varios archivos sueltos: se listan y se pide cuáles descargar.
    if too_big and len(too_big) == len(files):
        await status.edit_text(
            f"❌ Los {len(files)} archivos superan el máximo de {human_size(limit)} cada uno."
        )
        return
    view = services.folders.put(message.chat.id, message.from_user.id, url, tree_from_files(files))
    text, rows = menu_view(view)
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

    # -- navegación dentro del árbol ----------------------------------------------------
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
        view.page = page
        text, rows = menu_view(view)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action.startswith("s"):
        index = _index(action[1:])
        child = _child_at(view, index)
        if child is None or not child.is_dir:
            await callback.answer("Esa subcarpeta ya no está.", show_alert=True)
            return
        view.current = child
        view.page = 0
        text, rows = menu_view(view)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action == "u":
        parent = parent_of(view.root, view.current)
        if parent is None:
            await callback.answer("Ya estás en la raíz.", show_alert=True)
            return
        view.current = parent
        view.page = 0
        text, rows = menu_view(view)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action.startswith("f"):
        rest = action[1:]
        if rest.startswith("d") or rest.startswith("z"):
            index = _index(rest[1:])
            child = _child_at(view, index)
            if child is None or child.is_dir:
                await callback.answer("Ese archivo ya no está en el listado.", show_alert=True)
                return
            if rest.startswith("d"):
                await _send_file(callback, services, view, child)
            else:
                await _send_bundle(callback, services, view, bundle_for_file(child))
            return
        index = _index(rest)
        child = _child_at(view, index)
        if child is None or child.is_dir:
            await callback.answer("Ese archivo ya no está en el listado.", show_alert=True)
            return
        text, rows = file_view(view, index)
        await callback.answer()
        await _safe_edit_msg(msg, text, _kb(rows))
        return
    if action == "z":
        await _send_bundle(callback, services, view, bundle_for(view))
        return

    await callback.answer()


def _index(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return -1


def _child_at(view: FolderView, index: int) -> FolderNode | None:
    if index < 0 or index >= len(view.current.children):
        return None
    return view.current.children[index]


async def _send_file(callback: CallbackQuery, services: Services, view: FolderView, node: FolderNode) -> None:
    """Encola la descarga de un archivo suelto de la carpeta."""
    limit = services.settings.max_file_bytes
    if node.size and node.size > limit:
        await callback.answer(
            f"Pesa {human_size(node.size)}; el máximo es {human_size(limit)}.", show_alert=True
        )
        return
    source = resolve_source(view.url, services.http)
    if source is None:
        await callback.answer("No pude volver a resolver el enlace.", show_alert=True)
        return
    remote = node_to_file(node)
    pos = _submit(services, view.chat_id, view.user_id, source, remote)
    if pos is None:
        await callback.answer("Ya tienes demasiadas transferencias en cola.", show_alert=True)
        return
    await callback.answer("En cola ✓")
    await _safe_edit_msg(
        callback.message,
        f"📥 <b>{escape(remote.name)}</b>\nEn cola (posición {pos}). "
        "Puedes cerrar Telegram: te lo dejo aquí al terminar.",
    )


async def _send_bundle(
    callback: CallbackQuery, services: Services, view: FolderView, bundle: BundleFile
) -> None:
    """Encola el ZIP con la carpeta (o el archivo) elegido."""
    limit = services.settings.max_file_bytes
    oversized = [m for m in bundle.members if m.size and m.size > limit]
    if oversized:
        await callback.answer(
            f"«{oversized[0].name}» pesa {human_size(oversized[0].size)} "
            f"y el máximo es {human_size(limit)}.",
            show_alert=True,
        )
        return
    if (bundle.size or 0) > limit:
        await callback.answer(
            f"La carpeta pesa {human_size(bundle.size)} en total y el máximo es {human_size(limit)}. "
            "Elige una subcarpeta más pequeña.",
            show_alert=True,
        )
        return
    source = resolve_source(view.url, services.http)
    if source is None:
        await callback.answer("No pude volver a resolver el enlace.", show_alert=True)
        return
    pos = _submit(services, view.chat_id, view.user_id, source, bundle)
    if pos is None:
        await callback.answer("Ya tienes demasiadas transferencias en cola.", show_alert=True)
        return
    await callback.answer("Comprimiendo…")
    await _safe_edit_msg(
        callback.message,
        f"🗜 <b>{escape(bundle.name)}</b> — {len(bundle.members)} archivo(s) · "
        f"{human_size(bundle.size)}\nEn cola (posición {pos}). "
        "Puedes cerrar Telegram: te lo dejo aquí al terminar.",
    )
