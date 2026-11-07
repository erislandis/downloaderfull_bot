"""Entrega de archivos a Telegram troceados por la Bot API.

La Bot API de Telegram solo acepta documentos de hasta 50 MB. Para no depender de
MTProto ni de una cuenta extra, este módulo descarga el origen **en streaming** y
envía el resultado en partes algo menores que ``BOT_API_MAX_MB``; el usuario
recibe cada parte como un documento independiente en el mismo chat.

El archivo nunca se guarda entero: solo se mantiene en memoria la parte que se va
a enviar (más una parte de reserva para saber si hay más de una), por lo que el
consumo encaja en el plan gratuito de Render.

Si la descarga se corta, se reanuda desde el último byte leído (los orígenes Mega,
MediaFire y los enlaces directos permiten ``Range``). Así no se vuelve a descargar
lo que ya se había leído.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from html import escape
from typing import AsyncIterator, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from .config import Settings
from .errors import BridgeError, TransientError, UploadError
from .sources.base import RemoteFile, Source

log = logging.getLogger(__name__)

# Compatibilidad con el bot anterior (handler del puente MTProto). Ahora todas las
# entregas van por la Bot API, así que nunca se genera un pie de foto con este token.
BRIDGE_PREFIX = "bridge:"
PART_SEND_ATTEMPTS = 4
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_BASE_DELAY = 2.0
# Telegram responde HTTP 413 si el cuerpo multipart supera su límite. El archivo
# viaja dentro de un formulario, así que un trozo del tamaño exacto del límite ya
# no cabe: se reserva margen para el envelope y para la diferencia entre "50 MB"
# decimales y 50 MiB. Solo se aplica a límites grandes para no deformar los tests.
UPLOAD_SAFETY_MARGIN = 3 * 1024 * 1024
UPLOAD_MARGIN_MIN_LIMIT = 8 * 1024 * 1024

ProgressCallback = Callable[[int], Awaitable[None]]


@dataclass
class Timing:
    """Tiempos del último envío (para el informe /stats)."""

    download: float = 0.0
    upload: float = 0.0
    deliver: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0


def _bridge_caption(token: str | None) -> str | None:
    """Compatibilidad: el puente MTProto ya no se usa."""
    return f"{BRIDGE_PREFIX}{token}" if token else None


def part_filename(name: str, index: int) -> str:
    """Nombre de una parte conservando la extensión (``video.mp4`` → ``video.part001.mp4``)."""
    stem, ext = os.path.splitext(name)
    stem = stem[:180] or "archivo"
    return f"{stem}.part{index:03d}{ext}"


def _caption(name: str, index: int) -> str:
    return escape(f"{name} · parte {index}")


def _chunk_size(limit: int) -> int:
    """Tamaño efectivo de cada parte: deja sitio al envelope multipart de Telegram."""
    if limit > UPLOAD_MARGIN_MIN_LIMIT:
        return max(1, limit - UPLOAD_SAFETY_MARGIN)
    return limit


def _is_too_large(exc: BaseException) -> bool:
    """¿El error de Telegram es el 413 por cuerpo de petición demasiado grande?

    Se busca el texto del 413 y no un ``413`` suelto para no confundirlo con el
    ``retry after 413`` de un límite de ritmo.
    """
    text = str(exc).lower()
    return "entity too large" in text or "error_code\":413" in text or "413:" in text


async def _parts(
    source: Source,
    remote: RemoteFile,
    chunk_size: int,
    max_bytes: int,
    on_progress: ProgressCallback | None,
) -> AsyncIterator[tuple[int, bytes, bool, bool]]:
    """Parte el flujo en bloques de ``chunk_size`` y avisa si hay más de una parte.

    Retiene un bloque completo para saber si existe otro; así un archivo que cabe
    en una sola parte conserva su nombre original. Devuelve ``(índice, datos,
    es_último, hay_varias_partes)``.
    """
    buf = bytearray()
    pending: bytes | None = None
    index = 0
    downloaded = 0
    attempts = 0

    while True:
        try:
            async for piece in source.stream(remote, downloaded):
                downloaded += len(piece)
                if downloaded > max_bytes:
                    raise UploadError(
                        f"El archivo supera el máximo permitido ({max_bytes // (1024 * 1024)} MB)."
                    )
                buf += piece
                if on_progress is not None:
                    await on_progress(downloaded)
                while len(buf) >= chunk_size:
                    block = bytes(buf[:chunk_size])
                    del buf[:chunk_size]
                    if pending is not None:
                        index += 1
                        yield index, pending, False, True
                    pending = block
            break
        except TransientError as exc:
            attempts += 1
            if attempts > DOWNLOAD_ATTEMPTS:
                raise
            delay = DOWNLOAD_BASE_DELAY * (2 ** (attempts - 1))
            log.warning(
                "Se cortó la descarga de «%s» en %d bytes; reanudo en %d s (%s)",
                remote.name,
                downloaded,
                int(delay),
                exc,
            )
            await asyncio.sleep(delay)

    if pending is None:
        if not buf:
            raise UploadError("El archivo está vacío: no hay nada que enviar.")
        yield 1, bytes(buf), True, False
        return
    if buf:
        index += 1
        yield index, pending, False, True
        index += 1
        yield index, bytes(buf), True, True
        return
    index += 1
    yield index, pending, True, index > 1


class TelegramUploader:
    """Descarga el origen y publica el contenido en el chat, en partes menores que el límite."""

    def __init__(self, settings: Settings, bot: Bot) -> None:
        self._s = settings
        self._bot = bot
        self._bot_username: str | None = None
        self._current_task: asyncio.Task[object] | None = None
        self.last_timing = Timing()
        self.last_parts = 0
        self.last_error = ""

    @property
    def bot_username(self) -> str | None:
        return self._bot_username

    # -- ciclo de vida ------------------------------------------------------------------

    async def start(self) -> None:
        me = await self._bot.get_me()
        self._bot_username = me.username

    async def aclose(self) -> None:
        return None

    async def healthcheck(self) -> tuple[bool, str]:
        """La Bot API es la única vía de entrega: si el bot responde, está sano."""
        self.last_error = ""
        return True, ""

    async def cleanup(self, max_age: float | None) -> int:
        """No hay almacén intermedio que limpiar: las partes quedan en el chat del usuario."""
        return 0

    # -- puente MTProto (compatibilidad con el bot anterior) ------------------------------

    def resolve_bridge(self, token: str, message_id: int) -> bool:
        return False

    def cancel_current(self) -> None:
        task = self._current_task
        if task is not None and not task.done():
            task.cancel()

    # -- subida -------------------------------------------------------------------------

    async def upload(
        self,
        source: Source,
        remote: RemoteFile,
        chat_id: int,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        limit = self._s.bot_api_max_bytes
        if limit <= 0:
            raise UploadError("BOT_API_MAX_MB debe ser mayor que cero.")
        size = remote.size
        if size is not None and size > self._s.max_file_bytes:
            raise UploadError(
                f"El archivo pesa {size // (1024 * 1024)} MB y el máximo es "
                f"{self._s.max_file_bytes // (1024 * 1024)} MB."
            )

        chunk = _chunk_size(limit)
        started = time.monotonic()
        last_read = started
        downloaded = 0
        parts = 0
        self._current_task = asyncio.current_task()
        try:
            async for index, data, _last, multi in _parts(
                source, remote, chunk, self._s.max_file_bytes, on_progress
            ):
                last_read = time.monotonic()
                filename = part_filename(remote.name, index) if multi else remote.name
                kwargs = {"disable_content_type_detection": True}
                if multi:
                    kwargs["caption"] = _caption(remote.name, index)
                await self._send_with_retry(chat_id, filename, data, **kwargs)
                downloaded += len(data)
                parts = index
        except asyncio.CancelledError:
            raise
        except UploadError:
            raise
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"No pude enviar el archivo a Telegram: {exc}") from exc
        finally:
            self._current_task = None

        elapsed = time.monotonic() - started
        self.last_timing = Timing(
            download=max(0.0, last_read - started),
            upload=elapsed,
            bytes_downloaded=downloaded,
            bytes_uploaded=downloaded,
        )
        self.last_parts = parts
        return downloaded

    async def _send_with_retry(self, chat_id: int, filename: str, data: bytes, **kwargs) -> None:
        last: Exception | None = None
        for attempt in range(PART_SEND_ATTEMPTS):
            try:
                await self._bot.send_document(
                    chat_id,
                    BufferedInputFile(data, filename=filename),
                    **kwargs,
                )
                return
            except TelegramAPIError as exc:
                if _is_too_large(exc):
                    raise UploadError(
                        f"Telegram rechazó «{filename}» por tamaño (HTTP 413): el cuerpo "
                        "multipart supera el límite. Reduce BOT_API_MAX_MB para dejar margen."
                    ) from exc
                if isinstance(exc, TelegramRetryAfter):
                    last = exc
                    await asyncio.sleep(max(float(exc.retry_after), 1.0))
                elif isinstance(exc, TelegramNetworkError):
                    last = exc
                    await asyncio.sleep(DOWNLOAD_BASE_DELAY * (attempt + 1))
                else:
                    raise UploadError(f"Telegram rechazó «{filename}»: {exc}") from exc
        raise UploadError(f"No pude enviar «{filename}» a Telegram: {last}")
