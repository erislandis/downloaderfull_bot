"""Entrega de archivos por Telegram.

Estrategia híbrida:
* Archivos pequeños (≤ ``BOT_API_MAX_MB``): los envía el propio bot por la Bot API
  (se descargan en RAM, nunca en disco).
* Archivos grandes (hasta 2 GB): los sube una cuenta de usuario por MTProto. Como la
  cuenta no puede escribir a quien nunca la contactó, sube el documento al chat privado
  del bot y el bot lo **reenvía** al usuario (reenviar no vuelve a subir el archivo, así
  esquiva el límite de 50 MB de la Bot API).

La subida por MTProto corre en su propio hilo con su propio loop; el ``stream()`` del
origen sigue en el loop principal y alimenta un ``StreamReader`` acotado en memoria.

Los ZIP (varios archivos en uno) se construyen en un fichero temporal cuando no caben
en la Bot API: MTProto necesita conocer el tamaño final y leer de un fichero real, no
de un flujo en memoria.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from .config import Settings
from .errors import TransientError, UploadError
from .mtproto_upload import upload_document
from .sources.base import BundleFile, RemoteFile, Source
from .streams import StreamReader

if TYPE_CHECKING:  # pragma: no cover
    from telethon import TelegramClient

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int], Awaitable[None]]
PROGRESS_MIN_INTERVAL = 0.5
PART_SIZE_KB = 512  # máximo que acepta Telegram (mitad de peticiones que 256 KB)
DOWNLOAD_ATTEMPTS = 3  # reintentos de la descarga antes de tirar el trabajo
WRITE_BATCH = 1024 * 1024  # bytes que se acumulan antes de escribir en disco


@dataclass
class Timing:
    """Tiempos del último envío (para el informe /stats)."""

    download: float = 0.0
    upload: float = 0.0
    forward: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class TelegramUploader:
    def __init__(self, settings: Settings, bot: Bot) -> None:
        self._s = settings
        self._bot = bot
        self._client: TelegramClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._me_id: int | None = None
        self._bot_username: str | None = None
        self._bot_entity = None
        self._current_task: asyncio.Task[object] | None = None
        self._progress: ProgressCallback | None = None
        self._last_progress = 0.0
        self._start_error: BaseException | None = None
        self._storage: list[tuple[int, float]] = []
        self._storage_lock = threading.Lock()
        # Hilo exclusivo para volcar los bloques en el lector de la subida: así el
        # productor nunca compite con la compresión de los ZIP ni con el hash.
        self._feed_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="feed"
        )
        self.last_timing = Timing()
        self.last_error: str = ""

    @property
    def bot_username(self) -> str | None:
        """@username del bot (resuelto en ``start``)."""
        return self._bot_username

    # -- ciclo de vida ------------------------------------------------------------------

    async def start(self) -> None:
        self._main = asyncio.get_running_loop()
        me = await self._bot.get_me()
        if not me.username:
            raise UploadError("El bot necesita un @username para recibir las subidas grandes.")
        self._bot_username = me.username

        ready = threading.Event()
        self._thread = threading.Thread(target=self._run_thread, args=(ready,), name="telethon", daemon=True)
        self._thread.start()
        if not await asyncio.to_thread(ready.wait, 90):
            raise UploadError("La sesión MTProto tardó demasiado en conectar.")
        if self._start_error is not None:
            raise self._start_error

        user = await self._submit_and_wait(self._client.get_me())  # type: ignore[union-attr]
        self._me_id = user.id
        self._bot_entity = await self._submit_and_wait(self._client.get_entity(self._bot_username))  # type: ignore[union-attr]

    def _run_thread(self, ready: threading.Event) -> None:
        # Telethon se importa aquí y no al importar el módulo: en el plan gratuito de
        # Render la CPU es muy justa y la importación tarda decenas de segundos. Con el
        # import perezoso el servidor web abre el puerto mucho antes.
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._client = TelegramClient(
            StringSession(self._s.telegram_session), self._s.telegram_api_id, self._s.telegram_api_hash
        )
        try:
            loop.run_until_complete(self._client.connect())
            if not loop.run_until_complete(self._client.is_user_authorized()):
                self._start_error = UploadError("La sesión MTProto (TELEGRAM_SESSION) no es válida. Genérala de nuevo.")
        except Exception as exc:  # noqa: BLE001
            self._start_error = UploadError(f"No pude conectar con la sesión MTProto: {exc}")
        ready.set()
        if self._start_error is not None:
            loop.close()
            return
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(self._client.disconnect())
            loop.close()

    async def aclose(self) -> None:
        self._feed_executor.shutdown(wait=False, cancel_futures=True)
        if self._loop is None:
            return
        with contextlib.suppress(Exception):
            await self._submit_and_wait(self._client.disconnect())  # type: ignore[union-attr]
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 10)

    def _submit(self, coro) -> "concurrent.futures.Future":
        if self._loop is None:
            raise UploadError("La sesión MTProto no está disponible.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _submit_and_wait(self, coro):
        return await asyncio.wrap_future(self._submit(coro))

    # -- subida -------------------------------------------------------------------------

    async def upload(
        self, source: Source, remote: RemoteFile, chat_id: int, on_progress: ProgressCallback | None = None
    ) -> int:
        if isinstance(remote, BundleFile):
            return await self._upload_bundle(source, remote, chat_id, on_progress)
        size = remote.size
        if size is not None and size <= self._s.bot_api_max_bytes:
            return await self._via_bot(source, remote, chat_id, on_progress)
        return await self._via_mtproto(source, remote, chat_id, on_progress)

    async def _via_bot(
        self, source: Source, remote: RemoteFile, chat_id: int, on_progress: ProgressCallback | None
    ) -> int:
        started = time.monotonic()
        buf = bytearray()
        async for chunk in source.stream(remote):
            buf += chunk
            if on_progress is not None:
                await on_progress(len(buf))
        download_done = time.monotonic() - started
        if len(buf) > self._s.bot_api_max_bytes:
            raise UploadError("El archivo creció por encima del máximo de la Bot API; inténtalo de nuevo.")
        try:
            await self._bot.send_document(
                chat_id,
                BufferedInputFile(bytes(buf), filename=remote.name),
                disable_content_type_detection=True,
            )
        except TelegramNetworkError as exc:
            # Cortarse a mitad de subida es un fallo de red: se reintenta desde el principio.
            raise TransientError(f"La subida se cortó ({exc.__class__.__name__}); reintentando.") from exc
        except TelegramRetryAfter as exc:
            raise TransientError(f"Telegram pidió esperar {exc.retry_after} s; reintentando.") from exc
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"No pude enviar el archivo: {exc}") from exc
        self.last_timing = Timing(
            download=download_done,
            upload=time.monotonic() - started,
            bytes_downloaded=len(buf),
            bytes_uploaded=len(buf),
        )
        return len(buf)

    # -- ZIP (varios archivos en uno) ---------------------------------------------------

    async def _upload_bundle(
        self, source: Source, remote: BundleFile, chat_id: int, on_progress: ProgressCallback | None
    ) -> int:
        """Sube un ZIP. Si es pequeño va en RAM; si no, a un fichero temporal.

        El tamaño final de un ZIP con compresión no se conoce de antemano, así que para
        los ZIP grandes se construye primero en disco y luego se decide la vía de subida.
        """
        limit = self._s.bot_api_max_bytes
        total = remote.size or 0
        overhead = 256 * max(1, len(remote.members))  # cabeceras ZIP
        if total + overhead <= limit:
            # El ZIP no puede pesar más que la suma de sus miembros: cabe en memoria.
            return await self._via_bot(source, remote, chat_id, on_progress)

        if total > self._s.max_file_bytes:
            raise UploadError(
                f"La carpeta pesa {total} bytes y el máximo permitido es {self._s.max_file_bytes}."
            )
        self._check_disk_space(total)

        path, size = await self._write_temp(source, remote, on_progress)
        try:
            if size > self._s.max_file_bytes:
                raise UploadError(
                    f"El ZIP pesa {size} bytes y el máximo permitido es {self._s.max_file_bytes}."
                )
            if size <= limit:
                data = await asyncio.to_thread(_read_file, path)
                try:
                    await self._bot.send_document(
                        chat_id,
                        BufferedInputFile(data, filename=remote.name),
                        disable_content_type_detection=True,
                    )
                except TelegramNetworkError as exc:
                    raise TransientError(f"La subida se cortó ({exc.__class__.__name__}); reintentando.") from exc
                except Exception as exc:  # noqa: BLE001
                    raise UploadError(f"No pude enviar el ZIP: {exc}") from exc
                return size
            return await self._via_mtproto_file(path, remote, chat_id, on_progress, size)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    def _check_disk_space(self, needed: int) -> None:
        """El disco de Render es efímero y pequeño: no vale la pena empezar si no cabe."""
        try:
            free = shutil.disk_usage(tempfile.gettempdir()).free
        except OSError:  # pragma: no cover
            return
        if free < needed * 1.1:
            raise UploadError(
                "El servidor no tiene espacio libre suficiente para comprimir ese contenido "
                f"(quedan {free // (1024 * 1024)} MB). Prueba con una carpeta más pequeña."
            )

    async def _write_temp(
        self, source: Source, remote: RemoteFile, on_progress: ProgressCallback | None
    ) -> tuple[str, int]:
        """Escribe el contenido de un archivo (o el ZIP) en un fichero temporal."""
        fd, path = tempfile.mkstemp(prefix="bridgebot-", suffix=".zip" if isinstance(remote, BundleFile) else ".bin")
        written = 0
        batch = bytearray()
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in source.stream(remote):
                    batch += chunk
                    written += len(chunk)
                    if len(batch) >= WRITE_BATCH:
                        await asyncio.to_thread(fh.write, bytes(batch))
                        batch.clear()
                    if on_progress is not None:
                        await on_progress(written)
                if batch:
                    await asyncio.to_thread(fh.write, bytes(batch))
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise UploadError(f"No pude escribir el ZIP en el servidor: {exc}") from exc
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return path, written

    async def _via_mtproto(
        self, source: Source, remote: RemoteFile, chat_id: int, on_progress: ProgressCallback | None
    ) -> int:
        if self._client is None or self._loop is None or self._main is None or self._me_id is None:
            raise UploadError("La sesión MTProto no está disponible.")

        if remote.size is None:
            # Telethon necesita conocer el tamaño: si no lo sabe, lee el archivo entero
            # en memoria y el proceso muere de OOM en el plan gratuito de Render.
            path, size = await self._write_temp(source, remote, on_progress)
            try:
                return await self._via_mtproto_file(path, remote, chat_id, on_progress, size)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)

        reader = StreamReader(max_buffered=self._s.buffer_size * 4, name=remote.name)
        self._progress = on_progress
        produced = 0
        timing = Timing()
        started = time.monotonic()
        download_done = 0.0

        async def _produce() -> None:
            """Descarga el archivo y lo va metiendo en el lector de la subida.

            Si la descarga se corta (la CDN de Mega corta las conexiones largas) se
            reanuda desde el punto exacto en el que se quedó: la clave de Mega
            permite descifrar desde cualquier bloque.
            """
            nonlocal produced, download_done
            offset = 0
            attempts = 0
            while True:
                try:
                    async for chunk in source.stream(remote, offset):
                        produced += len(chunk)
                        offset = produced
                        await self._main.run_in_executor(self._feed_executor, reader.feed, chunk)
                    download_done = time.monotonic() - started
                    reader.finish()
                    return
                except TransientError as exc:
                    attempts += 1
                    if attempts > DOWNLOAD_ATTEMPTS:
                        reader.finish(exc)
                        return
                    log.warning(
                        "Se cortó la descarga de «%s» en %d/%d bytes; reanudo: %s",
                        remote.name,
                        produced,
                        remote.size or 0,
                        exc,
                    )
                    await asyncio.sleep(2**attempts)
                except BaseException as exc:  # noqa: BLE001
                    reader.finish(exc)
                    return

        producer = self._main.create_task(_produce())
        try:
            message = await asyncio.wrap_future(self._submit(self._run_upload(reader, remote)))
        except asyncio.CancelledError:
            self.cancel_current()
            raise
        except Exception as exc:  # noqa: BLE001
            if reader.error is not None:
                raise reader.error
            raise UploadError(f"No pude subir el archivo a Telegram: {exc}") from exc
        finally:
            # Cerrar desbloquea a `feed()` si el productor quedó esperando con la cola
            # llena (cancelación o error): sin esto, un hilo del pool se quedaría parado.
            reader.close()
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer
            self._progress = None

        if message is None:
            raise UploadError("Telegram no confirmó el documento.")
        timing.bytes_downloaded = produced
        timing.bytes_uploaded = remote.size or produced
        timing.download = download_done
        timing.upload = time.monotonic() - started
        await self._forward(message, chat_id)
        timing.forward = time.monotonic() - started - timing.upload
        self.last_timing = timing
        return produced or (remote.size or 0)

    async def _via_mtproto_file(
        self,
        path: str,
        remote: RemoteFile,
        chat_id: int,
        on_progress: ProgressCallback | None,
        size: int,
    ) -> int:
        """Sube un fichero ya escrito en disco (ZIP grande) por MTProto."""
        if self._client is None or self._loop is None or self._main is None or self._me_id is None:
            raise UploadError("La sesión MTProto no está disponible.")

        self._progress = on_progress
        timing = Timing()
        started = time.monotonic()
        try:
            message = await asyncio.wrap_future(self._submit(self._run_upload_path(path, remote, size)))
        except asyncio.CancelledError:
            self.cancel_current()
            raise
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"No pude subir el ZIP a Telegram: {exc}") from exc
        finally:
            self._progress = None

        if message is None:
            raise UploadError("Telegram no confirmó el documento.")
        timing.upload = time.monotonic() - started
        timing.bytes_downloaded = size
        timing.bytes_uploaded = size
        await self._forward(message, chat_id)
        timing.forward = time.monotonic() - started - timing.upload
        self.last_timing = timing
        return size

    async def _forward(self, message, chat_id: int) -> None:
        """Reenvía al usuario el documento ya subido al chat privado del bot.

        Reenviar no vuelve a subir el archivo, así que se puede reintentar sin coste:
        si el usuario está desconectado no pasa nada (Telegram le entrega el mensaje
        cuando vuelva).
        """
        last: Exception | None = None
        for attempt in range(3):
            try:
                await self._bot.forward_message(
                    chat_id=chat_id, from_chat_id=self._me_id, message_id=message.id
                )
            except TelegramNetworkError as exc:
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except Exception as exc:  # noqa: BLE001
                raise UploadError(f"Subí el archivo pero no pude reenviarlo: {exc}") from exc
            else:
                with self._storage_lock:
                    self._storage.append((message.id, time.monotonic()))
                return
        raise UploadError(f"Subí el archivo pero no pude reenviarlo: {last}")

    async def _run_upload(self, reader: StreamReader, remote: RemoteFile):
        """Sube el contenido del lector (ya alimentado por el productor)."""
        size = remote.size or 0
        if self._s.upload_parallelism > 1 and size > 0:
            return await self._run_upload_parallel(reader, remote, size)
        return await self._run_upload_sequential(reader, remote)

    async def _run_upload_parallel(self, reader: StreamReader, remote: RemoteFile, size: int):
        """Varias piezas en vuelo: es lo que quita la latencia de la subida."""
        handle = await upload_document(
            self._client,  # type: ignore[arg-type]
            self._bot_entity,
            reader,
            size=size,
            name=remote.name,
            parallelism=self._s.upload_parallelism,
            progress=self._on_progress_bytes,
        )
        # `handle` ya está subido: `send_file` solo lo publica, no lo vuelve a subir.
        self._current_task = asyncio.ensure_future(
            self._client.send_file(  # type: ignore[union-attr]
                self._bot_entity,
                file=handle,
                force_document=True,
                file_name=remote.name,
            )
        )
        try:
            return await self._current_task
        finally:
            self._current_task = None

    async def _run_upload_sequential(self, reader: StreamReader, remote: RemoteFile):
        """Subida pieza a pieza (la que hace Telethon por defecto)."""
        self._current_task = asyncio.ensure_future(
            self._client.send_file(  # type: ignore[union-attr]
                self._bot_entity,
                file=reader,
                force_document=True,
                file_name=remote.name,
                file_size=remote.size,
                part_size_kb=PART_SIZE_KB,
                progress_callback=self._on_progress_thread,
            )
        )
        try:
            return await self._current_task
        finally:
            self._current_task = None

    async def _run_upload_path(self, path: str, remote: RemoteFile, size: int):
        """Sube un fichero de disco: con piezas en paralelo si el tamaño lo permite."""
        if self._s.upload_parallelism > 1 and size > 0:
            with open(path, "rb") as fh:
                handle = await upload_document(
                    self._client,  # type: ignore[arg-type]
                    self._bot_entity,
                    fh,
                    size=size,
                    name=remote.name,
                    parallelism=self._s.upload_parallelism,
                    progress=self._on_progress_bytes,
                )
            self._current_task = asyncio.ensure_future(
                self._client.send_file(  # type: ignore[union-attr]
                    self._bot_entity,
                    file=handle,
                    force_document=True,
                    file_name=remote.name,
                )
            )
            try:
                return await self._current_task
            finally:
                self._current_task = None

        self._current_task = asyncio.ensure_future(
            self._client.send_file(  # type: ignore[union-attr]
                self._bot_entity,
                file=path,
                force_document=True,
                file_name=remote.name,
                file_size=size,
                part_size_kb=PART_SIZE_KB,
                progress_callback=self._on_progress_thread,
            )
        )
        try:
            return await self._current_task
        finally:
            self._current_task = None

    def _on_progress_bytes(self, current: int) -> None:
        """Progreso de la subida paralela (bytes ya confirmados por Telegram)."""
        cb = self._progress
        main = self._main
        if cb is None or main is None or main.is_closed():
            return
        now = time.monotonic()
        if now - self._last_progress < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress = now
        asyncio.run_coroutine_threadsafe(cb(current), main)

    def _on_progress_thread(self, current: int, total: int) -> None:
        cb = self._progress
        main = self._main
        if cb is None or main is None or main.is_closed():
            return
        now = time.monotonic()
        if (total is None or current < total) and now - self._last_progress < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress = now
        asyncio.run_coroutine_threadsafe(cb(current), main)

    def cancel_current(self) -> None:
        loop, task = self._loop, self._current_task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    # -- limpieza -----------------------------------------------------------------------

    async def healthcheck(self) -> tuple[bool, str]:
        """Comprueba la sesión MTProto y, si hace falta, la reconecta."""
        if self._client is None or self._loop is None:
            error = str(self._start_error) if self._start_error else "La sesión MTProto no está disponible."
            self.last_error = error
            return False, error
        try:
            if not self._client.is_connected():
                await self._submit_and_wait(self._client.connect())
            await self._submit_and_wait(self._client.get_me())
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False, str(exc)
        self.last_error = ""
        return True, ""

    async def cleanup(self, max_age: float | None) -> int:
        """Borra del chat del bot los archivos subidos (todos si ``max_age`` es None)."""
        now = time.monotonic()
        with self._storage_lock:
            if max_age is None:
                old = [mid for mid, _ in self._storage]
                self._storage = []
            else:
                old = [mid for mid, ts in self._storage if now - ts >= max_age]
                self._storage = [(mid, ts) for mid, ts in self._storage if now - ts < max_age]
        if not old or self._client is None:
            return 0
        try:
            await self._submit_and_wait(self._client.delete_messages(self._bot_entity, old))
        except Exception:  # noqa: BLE001
            log.warning("No pude borrar de Telegram %d archivo(s)", len(old), exc_info=True)
            return 0
        return len(old)
