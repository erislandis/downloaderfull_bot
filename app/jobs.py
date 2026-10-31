"""Cola de trabajos: un archivo a la vez (cuida la RAM del plan gratuito)."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from .config import Settings
from .errors import BridgeError, QuotaExceeded, TransientError
from .sources.base import BundleFile, RemoteFile, Source
from .uploader import TelegramUploader
from .utils import human_duration, human_size, progress_bar

log = logging.getLogger(__name__)
EDIT_INTERVAL = 4.0  # segundos mínimos entre ediciones del mensaje de progreso
RETRY_ATTEMPTS = 3  # reintentos por error transitorio (red, 5xx, rate-limit)
RETRY_BASE_DELAY = 2.0  # espera tras el primer reintento (se duplica cada vez)
HISTORY_PER_USER = 5  # envíos recientes guardados por usuario
HISTORY_USERS = 200  # usuarios con historial (tope de memoria)
MAX_JOBS_PER_USER = 3  # transferencias en cola por usuario (el bot es público)
BROADCAST_PAUSE = 0.05  # segundos entre avisos: Telegram limita ~30 mensajes/s


@dataclass
class Job:
    chat_id: int
    user_id: int
    source: Source
    files: list[RemoteFile]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    cancelled: bool = False
    state: str = "queued"  # queued | running | done | failed
    note: str = ""

    @property
    def label(self) -> str:
        """Nombre de lo que se está subiendo (el ZIP si es un paquete)."""
        if not self.files:
            return "?"
        return self.files[0].name

    @property
    def size(self) -> int | None:
        return self.files[0].size if self.files else None


class _Progress:
    def __init__(self, message: Message | None, header: str, total: int | None) -> None:
        self._message, self._header, self._total = message, header, total
        self._started = self._last_edit = time.monotonic()

    async def __call__(self, done: int) -> None:
        if self._message is None:
            return  # no se pudo avisar al usuario: se sigue subiendo igual
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
        self._history: dict[int, list[dict[str, object]]] = {}  # user_id -> últimos envíos
        self._users: dict[int, dict[str, object]] = {}  # user_id -> resumen de actividad
        self._history_lock = threading.Lock()
        # Pausa de la cola: el evento está puesto cuando la cola está activa.
        self._resume = asyncio.Event()
        self._resume.set()
        self._stats: dict[str, float] = {
            "ok": 0,
            "failed": 0,
            "bytes": 0,
            "seconds": 0.0,
            "download": 0.0,
            "upload": 0.0,
        }
        self._stats_lock = threading.Lock()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> bool:
        """Detiene la cola (el trabajo en curso termina). Devuelve True si cambió."""
        if self.paused:
            return False
        self._resume.clear()
        return True

    def resume(self) -> bool:
        """Reanuda la cola. Devuelve True si estaba pausada."""
        if not self.paused:
            return False
        self._resume.set()
        return True

    def stats_snapshot(self) -> dict[str, float]:
        with self._stats_lock:
            return dict(self._stats)

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

    def pending_for(self, user_id: int) -> int:
        """Transferencias en cola o en curso de un usuario."""
        return sum(1 for job in self._jobs if job.user_id == user_id)

    def queue_items(self) -> list[dict[str, object]]:
        """Todos los trabajos en cola o en curso, de cualquier usuario (para /cola)."""
        return [
            {
                "id": job.id,
                "user_id": job.user_id,
                "chat_id": job.chat_id,
                "name": job.label,
                "size": job.size,
                "state": job.state,
            }
            for job in self._jobs
        ]

    def cancel_all(self) -> int:
        """Cancela todos los trabajos, de todos los usuarios. Devuelve cuántos."""
        count = 0
        for job in self._jobs:
            if job.cancelled:
                continue
            job.cancelled = True
            count += 1
            if job is self._current and self._task:
                self._task.cancel()
        return count

    def user_stats(self) -> list[dict[str, object]]:
        """Resumen de actividad por usuario (para /usuarios)."""
        with self._history_lock:
            rows = [dict(row) for row in self._users.values()]
        rows.sort(key=lambda row: int(row.get("last") or 0), reverse=True)
        return rows

    def known_users(self) -> list[int]:
        """IDs de todos los usuarios que han usado el bot alguna vez."""
        with self._history_lock:
            return sorted(set(self._users) | set(self._history))

    async def broadcast(self, text: str) -> tuple[int, int]:
        """Envía un mensaje a todos los usuarios conocidos. Devuelve (enviados, fallidos)."""
        sent = failed = 0
        for user_id in self.known_users():
            try:
                await self._bot.send_message(user_id, text)
            except TelegramAPIError:
                failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
                log.warning("No pude enviar el aviso al usuario %d", user_id, exc_info=True)
            else:
                sent += 1
            await asyncio.sleep(BROADCAST_PAUSE)  # no saturar el límite de Telegram
        return sent, failed

    def cancel_user(self, user_id: int) -> int:
        count = 0
        for job in self._jobs:
            if job.user_id == user_id and not job.cancelled:
                job.cancelled = True
                count += 1
                if job is self._current and self._task:
                    self._task.cancel()
        return count

    def user_status(self, user_id: int) -> list[dict[str, object]]:
        """Estado de los trabajos del usuario: los activos y los últimos enviados.

        Permite que, al volver al chat, sepa si su archivo sigue subiendo, si ya está
        entregado o si falló (y por qué).
        """
        items: list[dict[str, object]] = []
        for job in self._jobs:
            if job.user_id != user_id:
                continue
            if job.state not in ("queued", "running"):
                continue  # ya está en el historial: no se repite
            items.append(
                {
                    "name": job.label,
                    "size": job.size,
                    "state": job.state,
                    "note": job.note,
                    "active": True,
                }
            )
        with self._history_lock:
            past = list(self._history.get(user_id, ()))
        items.extend(past)
        return items

    def _remember(self, job: Job, state: str, note: str = "") -> None:
        """Guarda el resultado de un envío para poder consultarlo después."""
        entry = {
            "name": job.label,
            "size": job.size,
            "state": state,
            "note": note,
            "active": False,
        }
        with self._history_lock:
            history = self._history.setdefault(job.user_id, [])
            history.insert(0, entry)
            del history[HISTORY_PER_USER:]
            if len(self._history) > HISTORY_USERS:
                # Límite de memoria: se olvidan los usuarios más antiguos.
                for old in list(self._history)[:-HISTORY_USERS]:
                    if old != job.user_id:
                        del self._history[old]
            # Resumen por usuario para los informes de administración.
            row = self._users.setdefault(
                job.user_id, {"id": job.user_id, "jobs": 0, "ok": 0, "failed": 0, "last": 0.0}
            )
            row["jobs"] = int(row["jobs"]) + 1
            if state == "done":
                row["ok"] = int(row["ok"]) + 1
            elif state == "failed":
                row["failed"] = int(row["failed"]) + 1
            row["last"] = time.time()
            if len(self._users) > HISTORY_USERS:
                for old in list(self._users)[:-HISTORY_USERS]:
                    if old != job.user_id:
                        del self._users[old]

    def _count(self, *, done: int, failed: bool) -> None:
        """Acumula las estadísticas del envío (para /stats)."""
        if failed:
            with self._stats_lock:
                self._stats["failed"] += 1
            return
        timing = getattr(self._uploader, "last_timing", None)
        upload_seconds = max(float(getattr(timing, "upload", 0.0) or 0.0), 0.0)
        download_seconds = min(float(getattr(timing, "download", 0.0) or 0.0), upload_seconds)
        with self._stats_lock:
            self._stats["ok"] += 1
            self._stats["bytes"] += max(int(done), 0)
            self._stats["seconds"] += upload_seconds
            self._stats["download"] += download_seconds
            self._stats["upload"] += max(upload_seconds - download_seconds, 0.0)

    async def run_forever(self) -> None:
        while True:
            await self._resume.wait()  # cola en pausa: no se toma ningún trabajo
            job = await self._queue.get()
            if self.paused:  # se pausó mientras esperábamos: el trabajo sigue en cola
                self._queue.put_nowait(job)
                continue
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
                    if self._task is not None:
                        # Si nos cancelan a *nosotros* (cierre de la app) el trabajo
                        # interno sigue vivo: se corta también.
                        self._task.cancel()
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
            is_bundle = isinstance(remote, BundleFile)
            icon = "🗜" if is_bundle else "📦"
            header = f"{icon} <b>{title}</b>" + (f" ({index}/{total})" if total > 1 else "")
            job.state = "running"
            try:
                status = await self._bot.send_message(
                    job.chat_id, f"{header}\n⏳ {'Comprimiendo…' if is_bundle else 'Preparando…'}"
                )
            except Exception:  # noqa: BLE001
                log.warning("No pude avisar al usuario del trabajo %s", job.id, exc_info=True)
                status = None  # se sigue igual: lo importante es entregar el archivo
            try:
                done = await self._upload(job, remote, status, header)
            except QuotaExceeded as exc:
                wait = f" Vuelve a intentarlo en ~{human_duration(exc.retry_after)}." if exc.retry_after else ""
                job.state = "failed"
                job.note = str(exc)
                self._remember(job, "failed", str(exc))
                self._count(done=0, failed=True)
                if status is not None:
                    await _safe_edit(status, f"{header}\n⚠️ {escape(str(exc))}{wait}")
                if total - index:
                    await self._notify(job.chat_id, "Detengo el resto de la carpeta por el límite de cuota.")
                return
            except BridgeError as exc:
                job.state = "failed"
                job.note = str(exc)
                self._remember(job, "failed", str(exc))
                self._count(done=0, failed=True)
                if status is not None:
                    await _safe_edit(status, f"{header}\n❌ {escape(str(exc))}")
                continue

            job.state = "done"
            job.note = ""
            self._remember(job, "done")
            self._count(done=done, failed=False)
            if status is not None:
                await _safe_edit(status, f"✅ <b>{title}</b> ({human_size(done)}) enviado como documento.")

    async def _upload(self, job: Job, remote: RemoteFile, status: Message | None, header: str) -> int:
        """Sube con reintentos ante errores transitorios (red, 5xx, rate-limit)."""
        attempts = max(1, self.retry_attempts)
        # El tamaño de un ZIP no se conoce hasta comprimirlo: se muestra solo lo transferido.
        total = None if isinstance(remote, BundleFile) else remote.size
        for attempt in range(attempts):
            try:
                return await self._uploader.upload(
                    job.source, remote, job.chat_id, _Progress(status, header, total) if status else None
                )
            except TransientError as exc:
                if attempt + 1 >= attempts:
                    raise
                delay = self.retry_base_delay * (2**attempt)
                if status is not None:
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
