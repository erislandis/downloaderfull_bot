"""Utilidades HTTP compartidas por los orígenes de enlaces genéricos (directo, Drive)."""
from __future__ import annotations

import re
from email.message import Message
from urllib.parse import unquote

import httpx

from ..errors import SourceError, TransientError


async def open_stream(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None, params: dict | None = None
) -> httpx.Response:
    """GET en streaming (el llamador debe cerrar la respuesta). Traduce los fallos de red."""
    try:
        request = client.build_request("GET", url, headers=headers, params=params)
        return await client.send(request, stream=True)
    except httpx.TooManyRedirects:
        raise SourceError("El enlace tiene demasiadas redirecciones.") from None
    except (httpx.UnsupportedProtocol, httpx.InvalidURL):
        raise SourceError("El enlace no es válido.") from None
    except httpx.HTTPError as exc:
        raise TransientError("No pude conectar con ese servidor; reintentando.") from exc


_EXTENDED_NAME_RE = re.compile(r"filename\*\s*=\s*([A-Za-z0-9._-]+)'[^']*'([^;]+)", re.I)


def filename_from_disposition(value: str | None) -> str | None:
    """Nombre de archivo de una cabecera Content-Disposition.

    Según la RFC 6266, ``filename*=UTF-8''...`` (con acentos, ñ…) tiene prioridad sobre
    ``filename="..."`` (que suele ser una versión ASCII simplificada). Drive envía las dos.
    """
    if not value:
        return None
    if m := _EXTENDED_NAME_RE.search(value):
        try:
            name = unquote(m.group(2).strip().strip('"'), encoding=m.group(1), errors="replace")
        except LookupError:  # codificación desconocida: se prueba con el nombre simple
            name = ""
        if name:
            return name
    message = Message()
    message["Content-Disposition"] = value
    try:
        name = message.get_filename()
    except Exception:  # noqa: BLE001  # cabecera mal formada: se ignora
        return None
    return name or None


def content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def is_html(response: httpx.Response) -> bool:
    """¿Es una página web y no un archivo? (una página con ``attachment`` sí es un archivo)"""
    if content_type(response) not in ("text/html", "application/xhtml+xml"):
        return False
    return "attachment" not in response.headers.get("content-disposition", "").lower()


def response_size(response: httpx.Response) -> int | None:
    """Tamaño total del archivo según las cabeceras (``None`` si no se puede saber)."""
    if response.status_code == 206:
        total = response.headers.get("content-range", "").rpartition("/")[2]
        return int(total) if total.isdigit() else None
    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        return None  # httpx descomprime: Content-Length ya no coincide con lo que se sube
    length = response.headers.get("content-length", "")
    return int(length) if length.isdigit() else None
