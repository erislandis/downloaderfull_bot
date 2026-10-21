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
    # control del servicio
    "admin",
    "reiniciar",
    "restart",
    "version",
    "ping",
    "stats",
    "logs",
    "test_mtproto",
    # cola y transferencias
    "cola",
    "usuarios",
    "pausar",
    "reanudar",
    "cancelar_todo",
    "cancelar_usuario",
    "limpiar",
    # comunicación
    "broadcast",
    # permisos
    "permitir",
    "revocar",
    "admins",
    # moderación
    "ban",
    "unban",
    "baneados",
)

# Prefijo de los callback_data de administración (confirmaciones).
ADMIN_CB_PREFIX = "rs:"

ADMIN_HELP = (
    "<b>🛠 Comandos de administración</b>\n"
    "<b>Servicio</b>\n"
    "/admin – estado general del bot\n"
    "/reiniciar – reinicia el servicio (pide confirmación)\n"
    "/version – versión y entorno del bot\n"
    "/ping – comprobación de vida y latencia\n"
    "/stats – estadísticas de uso y recursos\n"
    "/logs [n] – últimas líneas del registro\n"
    "/test_mtproto – comprueba la sesión de las subidas grandes\n"
    "<b>Cola</b>\n"
    "/cola – trabajos en cola y en curso\n"
    "/usuarios – usuarios y su actividad\n"
    "/pausar · /reanudar – detiene o reanuda la cola\n"
    "/cancelar_todo – cancela todas las transferencias\n"
    "/cancelar_usuario &lt;id&gt; – cancela las de un usuario\n"
    "/limpiar [horas] – borra del almacén de Telegram\n"
    "<b>Usuarios</b>\n"
    "/broadcast &lt;mensaje&gt; – avisa a todos los usuarios\n"
    "/permitir · /revocar · /admins – permisos de administración\n"
    "/ban · /unban · /baneados – bloqueo de usuarios"
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


def is_admin_callback(data: str | None) -> bool:
    """¿El callback_query pertenece a la zona de administración?"""
    return bool(data) and data.startswith(ADMIN_CB_PREFIX)


def parse_int(text: str | None, default: int | None = None) -> int | None:
    """Primer entero de un comando ("/limpiar 6" → 6). ``default`` si no hay ninguno."""
    if not text:
        return default
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return default
    raw = parts[1].strip().split()[0] if parts[1].strip() else ""
    try:
        return int(raw)
    except ValueError:
        return default


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


class BanRegistry:
    """IDs bloqueados: no pueden usar el bot.

    Como los permisos de administración, solo viven en memoria: un reinicio del
    servicio los borra. Para que un bloqueo sea permanente hay que guardarlo
    fuera (o volver a aplicarlo al arrancar).
    """

    def __init__(self, ids: Iterable[int] | None = None) -> None:
        self._ids: set[int] = set(ids or ())
        self._reason: dict[int, str] = {}

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._ids)

    def is_banned(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._ids

    def reason(self, user_id: int) -> str:
        return self._reason.get(user_id, "")

    def ban(self, user_id: int, reason: str = "") -> bool:
        """Bloquea a un usuario. Devuelve False si ya estaba bloqueado."""
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        self._reason[user_id] = reason
        return True

    def unban(self, user_id: int) -> bool:
        """Desbloquea a un usuario. Devuelve False si no lo estaba."""
        if user_id not in self._ids:
            return False
        self._ids.discard(user_id)
        self._reason.pop(user_id, None)
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


def format_version(
    *,
    version: str,
    python: str,
    platform: str,
    uptime_seconds: int,
    mtproto_ok: bool,
    mtproto_error: str,
) -> str:
    """Versión y entorno del bot (/version)."""
    mtproto = "✅ listo" if mtproto_ok else f"⚠️ {escape(mtproto_error or 'sin detalle')}"
    return "\n".join(
        [
            "<b>ℹ️ Versión</b>",
            f"· BridgeBot {escape(version)}",
            f"· Python {escape(python)}",
            f"· Sistema {escape(platform)}",
            f"· En marcha desde hace {human_duration(uptime_seconds)}",
            f"· MTProto: {mtproto}",
        ]
    )


def format_stats(
    *,
    uptime_seconds: int,
    jobs_ok: int,
    jobs_failed: int,
    bytes_sent: int,
    seconds_active: float,
    users: int,
    admins: int,
    queue_pending: int,
    queue_paused: bool,
    mtproto_ok: bool,
    memory_mb: float | None = None,
    disk_free_mb: int | None = None,
    upload_parallelism: int = 1,
    zip_level: int = 1,
) -> str:
    """Estadísticas de uso y recursos del servidor (/stats)."""
    speed = bytes_sent / seconds_active if seconds_active > 0 else 0.0
    lines = [
        "<b>📊 Estadísticas</b>",
        f"· En marcha desde hace {human_duration(uptime_seconds)}",
        f"· Transferencias: {jobs_ok} ok · {jobs_failed} fallida(s)",
        f"· Enviado: {human_size(bytes_sent)} · media {human_size(int(speed))}/s",
        f"· Usuarios conocidos: {users} · administradores: {admins}",
        f"· Cola: {queue_pending} pendiente(s)" + (" · ⏸ en pausa" if queue_paused else ""),
        f"· MTProto: {'✅ listo' if mtproto_ok else '⚠️ no disponible'}",
        f"· Trozos simultáneos: {upload_parallelism} · compresión ZIP: {zip_level or 'sin comprimir'}",
    ]
    if memory_mb is not None:
        lines.append(f"· Memoria en uso: {memory_mb:.0f} MB")
    if disk_free_mb is not None:
        lines.append(f"· Disco libre: {disk_free_mb} MB")
    return "\n".join(lines)


def format_logs(lines: list[str]) -> str:
    """Últimas líneas del registro (/logs)."""
    if not lines:
        return "📓 No hay líneas guardadas todavía."
    body = "\n".join(escape(line) for line in lines)
    return f"<b>📓 Últimas {len(lines)} líneas</b>\n<pre>{body}</pre>"


def format_bans(bans: list[Mapping[str, object]]) -> str:
    """Lista de usuarios bloqueados (/baneados)."""
    if not bans:
        return "🚫 No hay ningún usuario bloqueado."
    lines = [f"<b>🚫 Bloqueados ({len(bans)})</b>"]
    for ban in bans:
        reason = str(ban.get("reason") or "")
        suffix = f" — {escape(reason)}" if reason else ""
        lines.append(f"· <code>{ban.get('id')}</code>{suffix}")
    return "\n".join(lines)


def format_admins(admins: Iterable[int]) -> str:
    """Lista de administradores (/admins)."""
    ids = sorted(admins)
    if not ids:
        return "🛠 No hay ningún administrador configurado."
    return "\n".join(
        [f"<b>🛠 Administradores ({len(ids)})</b>"] + [f"· <code>{i}</code>" for i in ids]
    )
