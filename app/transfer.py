"""Puente origen -> Drive con reintentos y reanudación en ambos extremos."""
from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from typing import AsyncIterator, Awaitable, Callable, Protocol

import httpx

from .drive import DriveFile
from .errors import DriveError, Realign, ResumeNotSupported, TransientError
from .sources.base import RemoteFile, Source

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int], Awaitable[None]]


class _Session(Protocol):
    async def send(self, offset: int, data: bytes, last: bool, total: int | None) -> DriveFile | None: ...
    async def query_offset(self) -> int | DriveFile: ...


class _Drive(Protocol):
    async def start_upload(self, name: str, size: int | None, folder_id: str) -> _Session: ...


async def rechunk(stream: AsyncIterator[bytes], size: int) -> AsyncIterator[tuple[bytes, bool]]:
    """Reagrupa un flujo en bloques de `size` bytes marcando cuál es el último.

    Retiene un bloque para poder saber si el siguiente existe; así el último bloque
    (de cualquier tamaño) se envía con el total correcto aunque el tamaño fuera desconocido.
    """
    buf = bytearray()
    pending: bytes | None = None
    async with aclosing(stream):
        async for part in stream:
            buf += part
            while len(buf) >= size:
                chunk = bytes(buf[:size])
                del buf[:size]
                if pending is not None:
                    yield pending, False
                pending = chunk
    if buf:
        if pending is not None:
            yield pending, False
        yield bytes(buf), True
    elif pending is not None:
        yield pending, True
    else:
        yield b"", True  # archivo vacío


async def transfer(
    source: Source,
    remote: RemoteFile,
    drive: _Drive,
    folder_id: str,
    *,
    chunk_size: int,
    on_progress: ProgressCallback | None = None,
    max_attempts: int = 6,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DriveFile:
    session = await drive.start_upload(remote.name, remote.size, folder_id)
    offset, attempts = 0, 0
    while True:
        try:
            async with aclosing(rechunk(source.stream(remote, offset), chunk_size)) as chunks:
                async for data, last in chunks:
                    done = await session.send(offset, data, last, remote.size)
                    offset += len(data)
                    if on_progress:
                        await on_progress(offset)
                    if last:
                        if done is None:
                            raise DriveError("Drive no confirmó el archivo completo.")
                        return done
            raise DriveError("El origen terminó sin entregar el bloque final.")
        except Realign as realign:  # Drive guardó menos de lo enviado: no cuenta como fallo
            offset = realign.offset
        except ResumeNotSupported:
            attempts += 1
            if attempts >= max_attempts:
                raise TransientError("No se pudo reanudar la transferencia tras varios intentos.")
            log.info("Reanudación no soportada; reiniciando %s desde cero", remote.name)
            session = await drive.start_upload(remote.name, remote.size, folder_id)
            offset = 0
        except (httpx.HTTPError, TransientError) as exc:
            attempts += 1
            if attempts >= max_attempts:
                raise TransientError("La transferencia falló repetidamente; inténtalo más tarde.") from exc
            log.warning("Fallo temporal (%s); reintento %d/%d", exc, attempts, max_attempts)
            await sleep(min(2**attempts, 30))
            confirmed = await session.query_offset()
            if isinstance(confirmed, DriveFile):  # la subida ya se había completado
                return confirmed
            offset = confirmed
