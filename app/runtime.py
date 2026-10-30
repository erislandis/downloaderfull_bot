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
from .oauth import OAuthService
from .token_store import CHAT_MARKER, TokenStore

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
        self.tokens = TokenStore(
            settings.token_store_path,
            bot_token=settings.bot_token,
            client_id=settings.google_client_id or "",
            client_secret=settings.google_client_secret or "",
        )
        self.drive = DriveClient(settings, self._drive_http, self.tokens)
        self.oauth = OAuthService(settings, self.http, self.drive)
        self.bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.jobs = JobManager(self.bot, settings, self.drive)
        self.dp: Dispatcher = build_dispatcher(
            Services(settings, self.http, self.drive, self.jobs, runtime=self, oauth=self.oauth)
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def bootstrap(self) -> None:
        """Recupera el token guardado en Telegram si no hay ninguno local ni en el entorno."""
        if self.drive.has_auth:
            return
        token = await self._load_telegram_token()
        if token:
            self.drive.set_refresh_token(token)
            log.info("Refresh token de Google recuperado del mensaje fijado de Telegram")

    def _store_chat_ids(self) -> list[int]:
        if self.settings.oauth_store_chat_id is not None:
            return [self.settings.oauth_store_chat_id]
        return sorted(self.settings.allowed_user_ids)

    async def _load_telegram_token(self) -> str | None:
        for chat_id in self._store_chat_ids():
            try:
                chat = await self.bot.get_chat(chat_id)
                message = getattr(chat, "pinned_message", None)
                token = self.tokens.decode_from_chat(getattr(message, "text", None))
                if token:
                    return token
            except Exception:  # noqa: BLE001 - el almacenamiento en Telegram es opcional
                log.debug("No pude leer el token fijado en el chat %s", chat_id, exc_info=True)
        return None

    async def persist_drive_token(self, refresh_token: str, user_id: int | None = None) -> None:
        """Guarda el token en Telegram para que sobreviva a reinicios de Render."""
        chat_ids = self._store_chat_ids()
        chat_id = chat_ids[0] if chat_ids else user_id
        if chat_id is None:
            return
        try:
            chat = await self.bot.get_chat(chat_id)
            old = getattr(chat, "pinned_message", None)
            if old is not None and str(getattr(old, "text", "") or "").startswith(CHAT_MARKER):
                await self.bot.delete_message(chat_id, old.message_id)
            message = await self.bot.send_message(
                chat_id,
                "🔐 Token de Google de BridgeBot (cifrado). No borres este mensaje: se usa para reconectar "
                "Drive después de un reinicio. Puedes revocarlo con /desconectar.",
                disable_notification=True,
            )
            text = self.tokens.encode_for_chat(refresh_token)
            await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message.message_id)
            await self.bot.pin_chat_message(chat_id, message.message_id, disable_notification=True)
        except Exception:  # noqa: BLE001 - si falla, el archivo local sigue siendo el respaldo
            log.warning("No pude guardar el token en un mensaje fijado de Telegram", exc_info=True)

    async def forget_drive_token(self, user_id: int | None = None) -> None:
        self.drive.clear_refresh_token()
        chat_ids = self._store_chat_ids()
        if user_id is not None and user_id not in chat_ids:
            chat_ids.append(user_id)
        for chat_id in chat_ids:
            try:
                chat = await self.bot.get_chat(chat_id)
                message = getattr(chat, "pinned_message", None)
                if message is not None and str(getattr(message, "text", "") or "").startswith(CHAT_MARKER):
                    await self.bot.delete_message(chat_id, message.message_id)
            except Exception:  # noqa: BLE001
                log.debug("No pude borrar el token fijado en el chat %s", chat_id, exc_info=True)

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
            if self.drive.has_auth:
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
