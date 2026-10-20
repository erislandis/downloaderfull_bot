"""Cola de trabajos: un archivo a la vez (cuida la RAM del plan gratuito)."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from .config import Settings
from .errors import BridgeError, QuotaExceeded, TransientError
from .sources.base import RemoteFile, Source
from .uploader import TelegramUploader
from .utils import human_duration, human_size, progress_bar

log = logging.getLogger(__name__)
EDIT_INTERVAL = 4.0  # segundos mínimos entre ediciones del mensaje de progreso
RETRY_ATTEMPTS = 3  # reintentos por error transitorio (red, 5xx, rate-limit)
RETRY_BASE_DELAY = 2.0  # espera tras el primer reintento (se duplica cada vez)


@dataclass
class Job:
    chat_id: int
    user_id: int
    source: Source
    files: list[RemoteFile]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    cancelled: bool = False


class _Progress:
    def __init__(self, message: Message, header: str, total: int | None) -> None:
        self._message, self._header, self._total = message, header, total
        self._started = self._last_edit = time.monotonic()

    async def __call__(self, done: int) -> None:
        now = time.monotonic()
        if now - self._last_edit < EDIT_INTERVAL:
            return
        self._last_edit = now
        speed = done / max(now - self._started, 0.001)
        if self._total:
            line = f"{progress_bar(done / self._total)} {done * 100 // self._total}%\n{human_size(done)} / {human_size(self._total)}"
        else:
            line = human_size(done)
        await _safe_edit(self._message, f"{self._header}\n{line} · {human_size(int(speed))}/s")


async def _safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramAPIError:
        pass  # "message is not modified", flood control, etc.: el progreso es solo informativo


class JobManager:
    retry_attempts = RETRY_ATTEMPTS
    retry_base_delay = RETRY_BASE_DELAY

    def __init__(self, bot: Bot, settings: Settings, uploader: TelegramUploader) -> None:
        self._bot, self._s, self._uploader = bot, settings, uploader
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: list[Job] = []  # actual + en cola, en orden
        self._current: Job | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    @property
    def pending(self) -> int:
        """Trabajos en cola o en curso."""
        return len(self._jobs)

    @property
    def running(self) -> Job | None:
        """Trabajo que se está transfiriendo ahora mismo (si lo hay)."""
        return self._current

    def submit(self, job: Job) -> int:
        self._jobs.append(job)
        self._queue.put_nowait(job)
        return len(self._jobs)  # posición (1 = se ejecuta ya)

    def cancel_user(self, user_id: int) -> int:
        count = 0
        for job in self._jobs:
            if job.user_id == user_id and not job.cancelled:
                job.cancelled = True
                count += 1
                if job is self._current and self._task:
                    self._task.cancel()
        return count

    async def run_forever(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.cancelled:
                    await self._notify(job.chat_id, "🛑 Trabajo cancelado antes de empezar.")
                    continue
                self._current = job
                self._task = asyncio.create_task(self._run(job))
                try:
                    await self._task
                except asyncio.CancelledError:
                    self._uploader.cancel_current()
                    if not job.cancelled:
                        raise  # cierre real de la aplicación
                    await self._notify(job.chat_id, "🛑 Transferencia cancelada.")
                except Exception:  # noqa: BLE001
                    log.exception("Fallo inesperado en el trabajo %s", job.id)
                    await self._notify(job.chat_id, "❌ Error inesperado. Revisa los registros del servidor.")
            finally:
                self._current = self._task = None
                if job in self._jobs:
                    self._jobs.remove(job)

    # -- ejecución --------------------------------------------------------------------------

    async def _run(self, job: Job) -> None:
        total = len(job.files)
        for index, remote in enumerate(job.files, 1):
            title = escape(remote.name)
            header = f"📦 <b>{title}</b>" + (f" ({index}/{total})" if total > 1 else "")
            status = await self._bot.send_message(job.chat_id, f"{header}\n⏳ Preparando…")
            try:
                done = await self._upload(job, remote, status, header)
            except QuotaExceeded as exc:
                wait = f" Vuelve a intentarlo en ~{human_duration(exc.retry_after)}." if exc.retry_after else ""
                await _safe_edit(status, f"{header}\n⚠️ {escape(str(exc))}{wait}")
                if total - index:
                    await self._notify(job.chat_id, "Detengo el resto de la carpeta por el límite de cuota.")
                return
            except BridgeError as exc:
                await _safe_edit(status, f"{header}\n❌ {escape(str(exc))}")
                continue

            await _safe_edit(status, f"✅ <b>{title}</b> ({human_size(done)}) enviado como documento.")

    async def _upload(self, job: Job, remote: RemoteFile, status: Message, header: str) -> int:
        """Sube con reintentos ante errores transitorios (red, 5xx, rate-limit)."""
        attempts = max(1, self.retry_attempts)
        for attempt in range(attempts):
            try:
                return await self._uploader.upload(
                    job.source, remote, job.chat_id, _Progress(status, header, remote.size)
                )
            except TransientError as exc:
                if attempt + 1 >= attempts:
                    raise
                delay = self.retry_base_delay * (2**attempt)
                await _safe_edit(
                    status,
                    f"{header}\n⚠️ {escape(str(exc))} "
                    f"Reintento {attempt + 1}/{attempts - 1} en {human_duration(int(delay))}…",
                )
                await asyncio.sleep(delay)
        raise AssertionError("inalcanzable")

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramAPIError:
            log.warning("No pude enviar un mensaje a %s", chat_id, exc_info=True)
