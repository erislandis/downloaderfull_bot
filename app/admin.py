"""Administración del bot: quién puede ejecutar los comandos de control y sus textos.

Este módulo **no importa aiogram** a propósito: la lógica de permisos y los informes son
simples y así se pueden probar sin el framework instalado.
"""
from __future__ import annotations

from html import escape
from typing import Iterable, Mapping

from .utils import human_duration, human_size

# Comandos restringidos al administrador. El middleware de app/bot.py usa esta lista para
# decidir si un mensaje es un comando de administración (y entonces exigir permisos).
ADMIN_COMMANDS: tuple[str, ...] = (
    "admin",
    "cola",
    "usuarios",
    "cancelar_todo",
    "limpiar",
    "broadcast",
    "permitir",
    "revocar",
)

ADMIN_HELP = (
    "<b>🛠 Comandos de administración</b>\n"
    "/admin – estado general del bot\n"
    "/cola – trabajos en cola y en curso\n"
    "/usuarios – usuarios y su actividad\n"
    "/cancelar_todo – cancela todas las transferencias\n"
    "/limpiar – borra los archivos almacenados en Telegram\n"
    "/broadcast &lt;mensaje&gt; – avisa a todos los usuarios\n"
    "/permitir &lt;id&gt; – da permisos de administración a un ID\n"
    "/revocar &lt;id&gt; – quita esos permisos"
)


def command_of(text: str | None) -> str:
    """Nombre del comando de un mensaje ("" si no es un comando).

    Se ignoran la barra inicial, el sufijo "@bot" y las mayúsculas.
    """
    if not text:
        return ""
    head = text.strip().split(maxsplit=1)[0]
    if not head.startswith("/"):
        return ""
    return head[1:].split("@", 1)[0].strip().lower()


def is_admin_command(text: str | None) -> bool:
    """¿El mensaje es uno de los comandos restringidos al administrador?"""
    return command_of(text) in ADMIN_COMMANDS


def parse_user_id(text: str | None) -> int | None:
    """Extrae un ID de usuario de un comando ("/permitir 12345" → 12345)."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().split()[0] if parts[1].strip() else ""
    if not raw:
        return None
    try:
        user_id = int(raw)
    except ValueError:
        return None
    return user_id if user_id > 0 else None


class AdminRegistry:
    """IDs con permisos de administración.

    Se inicializa con la configuración (ADMIN_USER_IDS) y se puede modificar en caliente
    con /permitir y /revocar. Los cambios solo viven en memoria: para que sean
    permanentes hay que editar la variable de entorno y reiniciar.
    """

    def __init__(self, ids: Iterable[int] | None = None) -> None:
        self._ids: set[int] = set(ids or ())

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._ids)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._ids

    def grant(self, user_id: int) -> bool:
        """Añade un admin. Devuelve False si ya lo era."""
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        return True

    def revoke(self, user_id: int) -> bool:
        """Quita un admin. Devuelve False si no lo era."""
        if user_id not in self._ids:
            return False
        self._ids.discard(user_id)
        return True


# -- informes ---------------------------------------------------------------------------


def format_admin_panel(
    *,
    bot: str | None,
    uptime_seconds: int,
    queue_pending: int,
    queue_current: str | None,
    mtproto_ok: bool,
    mtproto_error: str,
    users: int,
    admins: int,
    max_file_bytes: int,
    sources: Iterable[str],
) -> str:
    """Panel de estado del bot (/admin)."""
    mtproto = "✅ listo" if mtproto_ok else f"⚠️ no disponible ({escape(mtproto_error or 'sin detalle')})"
    current = escape(queue_current) if queue_current else "—"
    lines = [
        "<b>🛠 Panel de administración</b>",
        f"· Bot: {escape(bot or '—')}",
        f"· En marcha desde hace {human_duration(uptime_seconds)}",
        f"· MTProto (archivos grandes): {mtproto}",
        f"· Cola: {queue_pending} pendiente(s) · ahora: {current}",
        f"· Usuarios conocidos: {users} · administradores: {admins}",
        f"· Límite por archivo: {human_size(max_file_bytes)}",
        f"· Sitios: {escape(', '.join(sources))}",
        "",
        ADMIN_HELP,
    ]
    return "\n".join(lines)


def format_queue(jobs: list[Mapping[str, object]]) -> str:
    """Listado de los trabajos en cola y en curso (/cola)."""
    if not jobs:
        return "📭 No hay ninguna transferencia en cola."
    lines = [f"<b>📋 Cola ({len(jobs)})</b>"]
    for job in jobs:
        state = str(job.get("state") or "queued")
        mark = "🔄 subiendo" if state == "running" else "⏳ en cola"
        name = escape(str(job.get("name") or "?"))
        size = job.get("size")
        weight = f" ({human_size(size)})" if isinstance(size, int) else ""
        lines.append(f"· {mark}: {name}{weight} — usuario {job.get('user_id')}")
    return "\n".join(lines)


def format_users(users: list[Mapping[str, object]]) -> str:
    """Resumen de actividad por usuario (/usuarios)."""
    if not users:
        return "👤 Todavía no ha usado el bot nadie."
    lines = [f"<b>👥 Usuarios ({len(users)})</b>"]
    for user in users:
        lines.append(
            f"· <code>{user.get('id')}</code> — {user.get('jobs')} envío(s), "
            f"{user.get('ok')} ok, {user.get('failed')} fallido(s)"
        )
    return "\n".join(lines)
