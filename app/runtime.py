"""Ensamblado de dependencias y tareas en segundo plano (compartido por webhook y polling)."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from . import __version__
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


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.time()
        self.bot_username: str | None = None
        self.http = httpx.AsyncClient(
            # `write` alto: subir 50 MB por la Bot API puede tardar más de 30 s y un
            # timeout a medio subir rompe la entrega (el trabajo no se pierde, se reintenta).
            timeout=httpx.Timeout(connect=30.0, read=90.0, write=600.0, pool=30.0),
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
        self.dp: Dispatcher = build_dispatcher(
            Services(settings, self.http, self.uploader, self.jobs, self.folders)
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
            },
            "sources": [s.label for s in SOURCES],
            "max_file_mb": self.settings.max_file_bytes // (1024 * 1024),
            "mtproto": {"ok": not self.mtproto_error, "error": self.mtproto_error},
        }

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
