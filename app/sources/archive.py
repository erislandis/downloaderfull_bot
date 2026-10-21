"""Empaquetado de varios archivos en un único ZIP, en streaming y sin disco.

El ZIP se escribe "al vuelo": cada miembro se comprime con ``deflate`` y se acompaña de un
*data descriptor* (flag 3), que es donde van el CRC y los tamaños definitivos. Así no hace
falta conocer el tamaño comprimido antes de empezar (imprescindible: el tamaño final de un
ZIP con compresión no se puede calcular de antemano) y la memoria se mantiene constante.

El resultado es un ZIP estándar: lo abren 7-Zip, WinRAR, el explorador de Windows y
``zipfile`` de Python.
"""
from __future__ import annotations

import asyncio
import struct
import time
import zlib
from typing import AsyncIterator, Callable, Sequence

from .base import RemoteFile

_LOCAL_SIG = 0x04034B50
_CENTRAL_SIG = 0x02014B50
_EOCD_SIG = 0x06054B50
_DESCRIPTOR_SIG = 0x08074B50

_FLAG_DATA_DESCRIPTOR = 0x08  # el CRC y los tamaños van en el descriptor final
_FLAG_UTF8 = 0x800  # los nombres están en UTF-8

_METHOD_STORE = 0
_METHOD_DEFLATE = 8

_VERSION = 20  # versión 2.0: es la mínima que admite el descriptor de datos
_MAX_ENTRIES = 0xFFFF
_MAX_SIZE = 0xFFFFFFFF  # por encima de esto haría falta ZIP64 (no lo soporta el límite actual)


class ArchiveError(Exception):
    """El archivo comprimido no se puede construir (demasiados archivos, etc.)."""


def _dos_datetime() -> tuple[int, int]:
    """Fecha y hora en el formato de MS-DOS que usa el ZIP."""
    now = time.localtime()
    date = ((now.tm_year - 1980) << 9) | (now.tm_mon << 5) | now.tm_mday
    clock = (now.tm_hour << 11) | (now.tm_min << 5) | (now.tm_sec // 2)
    return clock, date


def member_name(remote: RemoteFile) -> str:
    """Ruta del miembro dentro del ZIP (sin rutas absolutas ni "..")."""
    raw = (remote.path or remote.name).replace("\\", "/")
    parts = [p.strip() for p in raw.split("/") if p.strip() not in ("", ".", "..")]
    return "/".join(parts) or "archivo"


def _local_header(name: bytes, method: int, clock: int, date: int) -> bytes:
    return (
        struct.pack(
            "<IHHHHHIIIHH",
            _LOCAL_SIG,
            _VERSION,
            _FLAG_DATA_DESCRIPTOR | _FLAG_UTF8,
            method,
            clock,
            date,
            0,  # CRC (va en el descriptor)
            0,  # tamaño comprimido (idem)
            0,  # tamaño original (idem)
            len(name),
            0,  # sin campo extra
        )
        + name
    )


def _descriptor(crc: int, csize: int, usize: int) -> bytes:
    return struct.pack("<IIII", _DESCRIPTOR_SIG, crc & 0xFFFFFFFF, csize, usize)


def _central_entry(
    name: bytes, method: int, clock: int, date: int, crc: int, csize: int, usize: int, offset: int
) -> bytes:
    return (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            _CENTRAL_SIG,
            _VERSION,  # versión creada por
            _VERSION,  # versión necesaria
            _FLAG_DATA_DESCRIPTOR | _FLAG_UTF8,
            method,
            clock,
            date,
            crc,
            csize,
            usize,
            len(name),
            0,  # extra
            0,  # comentario
            0,  # número de disco
            0,  # atributos internos
            0,  # atributos externos
            offset,
        )
        + name
    )


def _eocd(entries: int, cd_size: int, cd_offset: int) -> bytes:
    return struct.pack("<IHHHHIIH", _EOCD_SIG, 0, 0, entries, entries, cd_size, cd_offset, 0)


async def zip_stream(
    members: Sequence[RemoteFile],
    opener: Callable[[RemoteFile], AsyncIterator[bytes]],
    compress: bool = True,
) -> AsyncIterator[bytes]:
    """Genera el ZIP byte a byte: `opener` da el contenido de cada miembro.

    `opener` se llama una vez por miembro y debe devolver un iterador asíncrono de bytes.
    """
    if len(members) > _MAX_ENTRIES:
        raise ArchiveError(f"Un ZIP admite como máximo {_MAX_ENTRIES} archivos (aquí hay {len(members)}).")

    clock, date = _dos_datetime()
    method = _METHOD_DEFLATE if compress else _METHOD_STORE
    central: list[bytes] = []
    offset = 0

    for remote in members:
        name = member_name(remote).encode("utf-8")
        header = _local_header(name, method, clock, date)
        entry_offset = offset
        yield header
        offset += len(header)

        crc = 0
        usize = 0
        csize = 0
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15) if compress else None
        async for chunk in opener(remote):
            if not chunk:
                continue
            crc = zlib.crc32(chunk, crc)
            usize += len(chunk)
            # La compresión se hace en un hilo: zlib libera el GIL y así el bucle de
            # eventos sigue respondiendo (en Render el plan gratuito tiene 0,5 CPU).
            if compressor is None:
                data = chunk
            else:
                data = await asyncio.to_thread(compressor.compress, chunk)
            csize += len(data)
            if data:
                yield data
        if compressor is not None:
            data = await asyncio.to_thread(compressor.flush)
            csize += len(data)
            if data:
                yield data

        descriptor = _descriptor(crc, csize, usize)
        yield descriptor
        offset += csize + len(descriptor)

        central.append(
            _central_entry(name, method, clock, date, crc, csize, usize, entry_offset)
        )
        if offset > _MAX_SIZE:
            raise ArchiveError("El ZIP supera el tamaño máximo admitido (haría falta ZIP64).")

    cd = b"".join(central)
    yield cd
    yield _eocd(len(central), len(cd), offset)
