"""Ensamblado de dependencias y tareas en segundo plano (compartido por webhook y polling)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .bot import Services, build_dispatcher
from .config import Settings
from .drive import DriveClient
from .jobs import JobManager

log = logging.getLogger(__name__)
CLEANUP_EVERY = 30 * 60
KEEPALIVE_EVERY = 4 * 60


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=90.0), follow_redirects=True, headers={"User-Agent": "BridgeBot/1.0"}
        )
        # Sin seguir redirecciones: Drive responde 308 "Resume Incomplete" y no debe tratarse como redirect.
        self._drive_http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=120.0, write=120.0), follow_redirects=False
        )
        self.drive = DriveClient(settings, self._drive_http)
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.jobs = JobManager(self.bot, settings, self.drive)
        self.dp: Dispatcher = build_dispatcher(Services(settings, self.http, self.drive, self.jobs))
        self._tasks: list[asyncio.Task[None]] = []

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
        await self.bot.session.close()
        await self.http.aclose()
        await self._drive_http.aclose()

    async def _cleanup_loop(self) -> None:
        max_age = timedelta(hours=self.settings.ttl_hours)
        while True:
            try:
                n = await self.drive.cleanup(max_age)
                if n:
                    log.info("Limpieza: %d archivo(s) borrados de Drive", n)
            except Exception:  # noqa: BLE001
                log.warning("Falló la limpieza de Drive", exc_info=True)
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
