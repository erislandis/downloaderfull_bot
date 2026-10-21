"""Ensamblado de dependencias y tareas en segundo plano (compartido por webhook y polling)."""
from __future__ import annotations

import asyncio
import collections
import logging
import platform
import shutil
import sys
import tempfile
import time
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from . import __version__
from .admin import AdminRegistry, BanRegistry
from .bot import Services, build_dispatcher
from .config import Settings
from .folders import FolderBrowser
from .jobs import JobManager
from .sources import SOURCES
from .uploader import TelegramUploader

log = logging.getLogger(__name__)
CLEANUP_EVERY = 30 * 60
KEEPALIVE_EVERY = 4 * 60
API_TIMEOUT = 900.0  # segundos: margen para subir 50 MB por la Bot API con poca banda
LOG_LINES = 300  # líneas de registro que se guardan para /logs


class RingBufferHandler(logging.Handler):
    """Guarda las últimas líneas del registro en memoria (para /logs)."""

    def __init__(self, capacity: int = LOG_LINES) -> None:
        super().__init__()
        self._lines: collections.deque[str] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:  # noqa: BLE001  # el registro nunca debe romper el bot
            pass

    def tail(self, count: int) -> list[str]:
        if count <= 0:
            return []
        return list(self._lines)[-count:]


PUBLIC_COMMANDS = (
    ("start", "Empezar"),
    ("ayuda", "Cómo usar el bot"),
    ("estado", "Tus transferencias"),
    ("cancelar", "Cancelar tus transferencias"),
)

ADMIN_COMMANDS_MENU = PUBLIC_COMMANDS + (
    ("admin", "Panel de administración"),
    ("reiniciar", "Reiniciar el servicio"),
    ("version", "Versión y entorno"),
    ("ping", "Comprobar que el bot responde"),
    ("stats", "Estadísticas de uso y recursos"),
    ("logs", "Últimas líneas del registro"),
    ("test_mtproto", "Comprobar las subidas grandes"),
    ("cola", "Transferencias en curso"),
    ("usuarios", "Usuarios y actividad"),
    ("pausar", "Pausar la cola"),
    ("reanudar", "Reanudar la cola"),
    ("cancelar_todo", "Cancelar todas las transferencias"),
    ("cancelar_usuario", "Cancelar las de un usuario"),
    ("limpiar", "Borrar archivos del almacén"),
    ("broadcast", "Avisar a todos los usuarios"),
    ("permitir", "Dar permisos de administración"),
    ("revocar", "Quitar permisos de administración"),
    ("admins", "Listar administradores"),
    ("ban", "Bloquear un usuario"),
    ("unban", "Desbloquear un usuario"),
    ("baneados", "Listar usuarios bloqueados"),
)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.time()
        self.bot_username: str | None = None
        self.http = httpx.AsyncClient(
            # `write` alto: subir 50 MB por la Bot API puede tardar más de 30 s y un
            # timeout a medio subir rompe la entrega (el trabajo no se pierde, se reintenta).
            timeout=httpx.Timeout(connect=30.0, read=90.0, write=600.0, pool=30.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30.0),
            follow_redirects=True,
            headers={"User-Agent": "BridgeBot/1.0"},
        )
        # La sesión de aiogram usa 60 s por defecto: subir 50 MB por la Bot API se corta
        # con poca banda (Render gratuito). Se sube el techo a 15 minutos.
        self.bot = Bot(
            settings.bot_token,
            session=AiohttpSession(timeout=API_TIMEOUT),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.uploader = TelegramUploader(settings, self.bot)
        self.jobs = JobManager(self.bot, settings, self.uploader)
        self.folders = FolderBrowser()
        # IDs con permisos de administración (pueden cambiarse en caliente con /permitir).
        self.admin = AdminRegistry(settings.admin_user_ids)
        # IDs bloqueados (también solo en memoria).
        self.bans = BanRegistry()
        # Registro en memoria para /logs.
        self.log_handler = RingBufferHandler()
        self.log_handler.setFormatter(logging.Formatter("%H:%M:%S %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(self.log_handler)
        self.dp: Dispatcher = build_dispatcher(
            Services(
                settings=settings,
                http=self.http,
                uploader=self.uploader,
                jobs=self.jobs,
                folders=self.folders,
                admin=self.admin,
                bans=self.bans,
                status=self.status,
                logs=self.log_handler.tail,
                resources=self.resources,
                restart=self.request_restart,
                healthcheck=self.healthcheck_mtproto,
                refresh_commands=self.sync_commands,
            )
        )
        self._tasks: list[asyncio.Task[None]] = []
        self.mtproto_error: str = ""

    async def start(self) -> None:
        """Conecta la sesión MTProto y resuelve la identidad del bot (una sola vez).

        Si la sesión MTProto falla, el bot **arranca igual**: podrá subir archivos por la
        Bot API (≤50 MB) y avisar del problema en la página de estado. Solo se pierde la
        subida de archivos grandes.
        """
        try:
            await self.uploader.start()
        except Exception as exc:  # noqa: BLE001
            self.mtproto_error = str(exc)
            log.error("Sesión MTProto no disponible: %s", exc)
        else:
            self.bot_username = self.uploader.bot_username
        await self.sync_commands()

    async def sync_commands(self) -> None:
        """Publica el menú de comandos (y uno ampliado por chat de administrador)."""
        public = [BotCommand(command=name, description=desc) for name, desc in PUBLIC_COMMANDS]
        admin_menu = [BotCommand(command=name, description=desc) for name, desc in ADMIN_COMMANDS_MENU]
        try:
            await self.bot.set_my_commands(public)
        except Exception:  # noqa: BLE001
            log.warning("No pude publicar el menú de comandos", exc_info=True)
        for admin_id in self.admin.ids:
            try:
                await self.bot.set_my_commands(admin_menu, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:  # noqa: BLE001
                log.debug("No pude publicar el menú de administración para %s", admin_id, exc_info=True)

    def status(self) -> dict:
        """Instantánea del estado para la página web y la API de status."""
        job = self.jobs.running
        return {
            "ok": True,
            "status": "online",
            "version": __version__,
            "bot": self.bot_username,
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "queue": {
                "pending_jobs": self.jobs.pending,
                "current_file": job.files[0].name if job and job.files else None,
                "paused": self.jobs.paused,
            },
            "sources": [s.label for s in SOURCES],
            "max_file_mb": self.settings.max_file_bytes // (1024 * 1024),
            "mtproto": {"ok": not self.mtproto_error, "error": self.mtproto_error},
        }

    def environment(self) -> dict[str, Any]:
        """Versión y entorno para /version."""
        return {
            "version": __version__,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "mtproto_ok": not self.mtproto_error,
            "mtproto_error": self.mtproto_error,
        }

    def resources(self) -> dict[str, Any]:
        """Memoria y disco disponibles (para /stats)."""
        memory_mb: float | None = None
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux/macOS lo dan en KB; macOS en bytes.
            memory_mb = usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
        except Exception:  # noqa: BLE001  # plataformas sin `resource`
            pass
        disk_free_mb: int | None = None
        try:
            disk_free_mb = shutil.disk_usage(tempfile.gettempdir()).free // (1024 * 1024)
        except OSError:  # pragma: no cover
            pass
        return {"memory_mb": memory_mb, "disk_free_mb": disk_free_mb}

    async def request_restart(self) -> tuple[str, str]:
        """Reinicia el servicio.

        Devuelve ``(modo, detalle)``: ``deploy`` si lanzó el *deploy hook* de Render,
        ``exit`` si hay que terminar el proceso (Render lo vuelve a levantar).
        """
        hook = self.settings.render_deploy_hook_url
        if hook:
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(hook)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("El deploy hook de Render falló (%s); reinicio el proceso", exc)
            else:
                return "deploy", ""
        return "exit", ""

    async def healthcheck_mtproto(self) -> tuple[bool, str]:
        """Comprueba la sesión de las subidas grandes y actualiza el estado."""
        ok, detail = await self.uploader.healthcheck()
        self.mtproto_error = "" if ok else detail
        return ok, detail

    def start_background(self) -> None:
        self._tasks = [
            asyncio.create_task(self.jobs.run_forever(), name="jobs"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup"),
        ]
        if self.settings.webhook_base_url:
            self._tasks.append(asyncio.create_task(self._keepalive_loop(), name="keepalive"))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.uploader.aclose()
        await self.bot.session.close()
        await self.http.aclose()
        logging.getLogger().removeHandler(self.log_handler)

    async def _cleanup_loop(self) -> None:
        max_age = self.settings.ttl_hours * 3600
        while True:
            try:
                n = await self.uploader.cleanup(max_age)
                if n:
                    log.info("Limpieza: %d archivo(s) borrados de Telegram", n)
            except Exception:  # noqa: BLE001
                log.warning("Falló la limpieza de Telegram", exc_info=True)
            await asyncio.sleep(CLEANUP_EVERY)

    async def _keepalive_loop(self) -> None:
        """Mientras hay trabajos, genera tráfico entrante para que Render no duerma el servicio."""
        url = f"{self.settings.webhook_base_url}/healthz"
        while True:
            await asyncio.sleep(KEEPALIVE_EVERY)
            if self.jobs.busy:
                try:
                    await self.http.get(url)
                except httpx.HTTPError:
                    log.debug("Keepalive falló", exc_info=True)
