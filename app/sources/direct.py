"""Origen genérico: cualquier enlace http(s) que lleve directamente a un archivo.

Va el último en ``SOURCES`` (acepta cualquier URL). Averigua nombre y tamaño con una petición
``Range: bytes=0-0`` (muchos servidores no soportan HEAD), rechaza las páginas web y reanuda
las descargas cortadas con ``Range`` cuando el servidor lo permite.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import httpx

from ..errors import ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from ._guard import guarded_client
from ._http import (
    body_is_html,
    content_type,
    filename_from_disposition,
    is_html,
    open_stream,
    page_title,
    peek,
    response_size,
)
from .base import Inspection, RemoteFile, Source

READ_CHUNK = 256 * 1024
HTML_MESSAGE = (
    "Ese enlace lleva a una página web, no directamente a un archivo. "
    "Necesito un enlace de descarga directa (el que empieza a descargar al pulsarlo)."
)
BLOCK_HINTS = (
    "blocked",
    "block",
    "forbidden",
    "denied",
    "unauthorized",
    "captcha",
    "cloudflare",
    "security check",
    "just a moment",
    "too many requests",
    "rate limit",
    "access denied",
)


def html_message(title: str = "", body: bytes = b"") -> str:
    """Explica por qué el enlace no vale: qué página vio el bot y qué hacer."""
    message = HTML_MESSAGE
    if title:
        message += f' La página se titula "{title}".'
    haystack = f"{title} {body[:2000].decode('utf-8', 'replace')}".lower()
    if any(hint in haystack for hint in BLOCK_HINTS):
        message += (
            " Parece que ese servidor bloquea las descargas desde servidores en la nube: "
            "prueba con otro mirror o con el enlace de descarga directa."
        )
    return message


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
            probe = await _probe(client, url, {"Range": "bytes=0-0"})
            if probe.status == 416:  # Range sobre un archivo vacío
                raise SourceError("El archivo está vacío.")
            check_status(probe.status)
            if probe.html:
                # Hay servidores que responden con su página de inicio cuando no
                # entienden la petición con "Range": se reintenta sin esa cabecera.
                probe = await _probe(client, url, None)
                if probe.status == 416:
                    raise SourceError("El archivo está vacío.")
                check_status(probe.status)
                if probe.html:
                    raise SourceError(html_message(probe.title, probe.body))
            if probe.size == 0:
                raise SourceError("El archivo está vacío.")
            return Inspection(files=[RemoteFile(name=probe.name, size=probe.size, ref=url)])

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
                first = True
                try:
                    async for chunk in response.aiter_bytes(READ_CHUNK):
                        if first:
                            first = False
                            if body_is_html(chunk):
                                # El servidor dijo que era un binario pero Page el HTML:
                                # no se sube la web, se avisa.
                                raise SourceError(html_message(page_title(chunk), chunk))
                        yield chunk
                except httpx.HTTPError as exc:
                    raise TransientError("Se cortó la conexión con el servidor; reintentando.") from exc
            finally:
                await response.aclose()


@dataclass
class _Probe:
    """Lo que se averigua con una petición de sondeo."""

    status: int
    size: int | None
    name: str
    html: bool
    title: str
    body: bytes


async def _probe(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None) -> _Probe:
    """Sondea la URL: cabeceras, nombre, tamaño y si lo que hay es una página web."""
    response = await open_stream(client, url, headers)
    try:
        status = response.status_code
        if status == 416:
            return _Probe(status=status, size=0, name="", html=False, title="", body=b"")
        html = is_html(response)
        body = b""
        if html or content_type(response) in ("", "application/octet-stream", "binary/octet-stream"):
            # El servidor no siempre anuncia el tipo (o se equivoca): se mira el principio.
            body = await peek(response)
            html = html or body_is_html(body)
        return _Probe(
            status=status,
            size=response_size(response),
            name=name_from_response(response),
            html=html,
            title=page_title(body),
            body=body,
        )
    finally:
        await response.aclose()
