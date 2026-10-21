"""Subida de documentos por MTProto con varios trozos en vuelo.

``client.send_file()`` de Telethon sube el archivo trozo a tropezo **esperando la
respuesta del servidor antes de enviar el siguiente**
(``telethon/client/uploads.py``): un archivo de 500 MB son unas 2.000 ida y vuelta
y, con la latencia de un centro de datos, lo que manda no es el ancho de banda
sino esas esperas.

Este módulo lee trozos de 512 KB de un ``StreamReader`` y lanza varios
``SaveBigFilePartRequest`` a la vez: el centro de datos de Telegram tiene varias
piezas en vuelo y la conexión no se queda ociosa entre una y otra. La lectura se
hace en un hilo aparte para que el bucle de Telethon pueda atender las subidas
en curso mientras se prepara el siguiente trozo.

Al terminar devuelve un ``InputFileBig``: pasárselo a ``send_file()`` hace que
Telethon **no vuelva a subir** el archivo, solo lo publique en el chat.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from .errors import UploadError

if TYPE_CHECKING:  # pragma: no cover
    from telethon.tl import types

log = logging.getLogger(__name__)

PART_SIZE = 512 * 1024  # máximo que acepta Telegram en una pieza
MAX_PARALLELISM = 8
PART_ATTEMPTS = 3  # reintentos por pieza (una pieza caída no tira el archivo entero)

ProgressCallback = Callable[[int], None]


async def upload_document(
    client: Any,
    endpoint: Any,
    reader: Any,
    *,
    size: int,
    name: str,
    part_size: int = PART_SIZE,
    parallelism: int = 4,
    progress: ProgressCallback | None = None,
) -> types.InputFileBig:
    """Sube el contenido de ``reader`` (un ``StreamReader``) a ``endpoint``.

    Devuelve el ``InputFileBig`` ya subido, listo para enviarse con ``send_file``.
    """
    # Telethon se importa aquí (y no al importar el módulo): en el plan gratuito de
    # Render la CPU es muy justa y arrastrar Telethon al arrancar retrasa la apertura
    # del puerto del servidor web.
    from telethon import helpers
    from telethon.tl import types

    if size <= 0:
        raise UploadError("El archivo está vacío: no hay nada que subir.")
    part_size = max(1024, min(int(part_size), PART_SIZE))
    parallelism = max(1, min(int(parallelism), MAX_PARALLELISM))
    part_count = (size + part_size - 1) // part_size
    file_id = helpers.generate_random_long()

    pending: set[asyncio.Task[None]] = set()
    uploaded = 0

    def _report(amount: int) -> None:
        nonlocal uploaded
        uploaded += amount
        if progress is not None:
            progress(uploaded)

    try:
        for index in range(part_count):
            part = await _read_part(reader, part_size)
            if not part:
                raise UploadError("El origen se quedó sin datos antes de terminar el archivo.")
            task = asyncio.create_task(
                _send_part(client, file_id, index, part_count, part, _report),
                name=f"mtproto-part-{index}",
            )
            pending.add(task)
            task.add_done_callback(pending.discard)
            if len(pending) >= parallelism:
                await _wait_one(pending)
        while pending:
            await _wait_one(pending)
    except BaseException:
        for task in list(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise

    if uploaded < size:
        log.debug("Subida MTProto: %d de %d bytes confirmados", uploaded, size)
    return types.InputFileBig(file_id, part_count, name)


async def _read_part(reader: Any, part_size: int) -> bytes:
    """Lee un trozo sin bloquear el bucle de eventos (el lector es bloqueante)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, reader.read, part_size)


async def _wait_one(pending: set[asyncio.Task[None]]) -> None:
    """Espera a que termine una pieza y propaga su error si lo hubo.

    Se leen los resultados de todas las que hayan acabado a la vez para que ninguna
    excepción quede sin recoger (Python avisaría de "exception was never retrieved").
    """
    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    error: BaseException | None = None
    for task in done:
        pending.discard(task)
        if error is not None:
            continue
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001  # se relanza tras recoger las demás
            error = exc
    if error is not None:
        raise error


async def _send_part(
    client: Any,
    file_id: int,
    index: int,
    part_count: int,
    part: bytes,
    report: Callable[[int], None] | None = None,
) -> None:
    """Envía una pieza, reintentando si el fallo es pasajero."""
    from telethon.errors import FloodWaitError, ServerError
    from telethon.tl import functions

    request = functions.upload.SaveBigFilePartRequest(file_id, index, part_count, part)
    delay = 1.0
    for attempt in range(PART_ATTEMPTS):
        try:
            result = await client(request)
        except FloodWaitError as exc:
            wait = float(getattr(exc, "seconds", 5) or 5)
            log.warning("Telegram pidió esperar %.0f s antes de continuar la subida", wait)
            await asyncio.sleep(wait)
            continue
        except (asyncio.TimeoutError, OSError, ServerError) as exc:
            if attempt + 1 >= PART_ATTEMPTS:
                raise UploadError(f"Se cortó la subida del archivo ({exc}).") from exc
            log.warning(
                "Fallo transitorio subiendo la pieza %d/%d: %s. Reintento %d.",
                index + 1,
                part_count,
                exc,
                attempt + 1,
            )
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if not result:
            raise UploadError("Telegram rechazó una parte del archivo.")
        if report is not None:
            report(len(part))
        return
    raise UploadError("No pude subir una parte del archivo tras varios intentos.")
