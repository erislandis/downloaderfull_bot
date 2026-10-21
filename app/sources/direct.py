"""Origen genérico: cualquier enlace http(s) que lleve directamente a un archivo.

Va el último en ``SOURCES`` (acepta cualquier URL). Averigua nombre y tamaño con una petición
``Range: bytes=0-0`` (muchos servidores no soportan HEAD), rechaza las páginas web y reanuda
las descargas cortadas con ``Range`` cuando el servidor lo permite.
"""
from __future__ import annotations

import mimetypes
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import httpx

from ..errors import ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from ._guard import guarded_client
from ._http import content_type, filename_from_disposition, is_html, open_stream, response_size
from .base import Inspection, RemoteFile, Source

READ_CHUNK = 256 * 1024
HTML_MESSAGE = (
    "Ese enlace lleva a una página web, no directamente a un archivo. "
    "Necesito un enlace de descarga directa (el que empieza a descargar al pulsarlo)."
)


def check_status(status: int) -> None:
    if status in (401, 403):
        raise SourceError("El servidor rechazó la descarga (acceso denegado o enlace caducado).")
    if status in (404, 410):
        raise SourceError("El archivo no existe o fue eliminado.")
    if status == 429 or status >= 500:
        raise TransientError("El servidor está limitando o fallando; reintentando.")
    if status >= 400:
        raise SourceError(f"El servidor respondió con el error HTTP {status}.")


def name_from_response(response: httpx.Response) -> str:
    """Nombre del archivo: Content-Disposition, si no el final de la URL, si no el tipo MIME."""
    name = filename_from_disposition(response.headers.get("content-disposition"))
    if not name:
        name = unquote(urlparse(str(response.url)).path.rstrip("/").rsplit("/", 1)[-1])
    name = safe_filename(name, "archivo")
    if "." not in name:
        ctype = content_type(response)
        extension = mimetypes.guess_extension(ctype) if ctype and ctype != "application/octet-stream" else None
        if extension:
            name += extension
    return name


class DirectSource(Source):
    key = "direct"
    label = "Enlace directo"

    @classmethod
    def matches(cls, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)

    async def inspect(self, url: str) -> Inspection:
        async with guarded_client() as client:
            response = await open_stream(client, url, {"Range": "bytes=0-0"})
            try:
                if response.status_code == 416:  # Range sobre un archivo vacío
                    raise SourceError("El archivo está vacío.")
                check_status(response.status_code)
                if is_html(response):
                    raise SourceError(HTML_MESSAGE)
                size = response_size(response)
                name = name_from_response(response)
            finally:
                await response.aclose()
        if size == 0:
            raise SourceError("El archivo está vacío.")
        return Inspection(files=[RemoteFile(name=name, size=size, ref=url)])

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        headers = {"Range": f"bytes={offset}-"} if offset else None
        async with guarded_client() as client:
            response = await open_stream(client, file.ref, headers)
            try:
                if offset and response.status_code != 206:
                    raise ResumeNotSupported("Ese servidor no permite reanudar la descarga.")
                check_status(response.status_code)
                if is_html(response):
                    raise SourceError(HTML_MESSAGE)
                try:
                    async for chunk in response.aiter_bytes(READ_CHUNK):
                        yield chunk
                except httpx.HTTPError as exc:
                    raise TransientError("Se cortó la conexión con el servidor; reintentando.") from exc
            finally:
                await response.aclose()
