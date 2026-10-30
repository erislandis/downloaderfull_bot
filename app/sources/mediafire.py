from __future__ import annotations

import base64
import html as htmllib
import re
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import httpx

from ..errors import ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from .base import RemoteFile, Source

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
READ_CHUNK = 256 * 1024

_BUTTON_RE = re.compile(r"""<a\b[^>]*\bid\s*=\s*["']downloadButton["'][^>]*>""", re.I)
_FALLBACK_RE = re.compile(r"""https?://download[^"'\s<>]*mediafire\.com/[^"'\s<>]+""", re.I)
_FILENAME_RE = re.compile(
    r"""<div[^>]*class\s*=\s*["'][^"']*\bfilename\b[^"']*["'][^>]*>(.*?)</div>""", re.I | re.S
)
_ERROR_MARKERS = (
    ("dangerous file blocked", "MediaFire bloqueó este archivo por considerarlo peligroso."),
    ("file removed for violation", "El archivo fue eliminado de MediaFire por infringir sus normas."),
    ("invalid or deleted file", "El archivo no existe o fue eliminado."),
    ("the key you provided for file download was invalid", "El enlace de MediaFire no es válido."),
    ('type="password"', "El archivo está protegido con contraseña (no soportado)."),
)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""(?<![\w-]){re.escape(name)}\s*=\s*["']([^"']*)["']""", tag, re.I)
    return m.group(1) if m else None


def parse_direct_url(page: str) -> str | None:
    """Extrae el enlace directo del HTML de MediaFire (función pura, fácil de testear)."""
    m = _BUTTON_RE.search(page)
    if m:
        tag = m.group(0)
        scrambled = _attr(tag, "data-scrambled-url")
        if scrambled:
            try:
                decoded = base64.b64decode(scrambled + "=" * (-len(scrambled) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                decoded = ""
            if decoded.startswith("http"):
                return decoded
        href = _attr(tag, "href")
        if href and href.startswith("http"):
            return htmllib.unescape(href)
    m = _FALLBACK_RE.search(page)
    return htmllib.unescape(m.group(0)) if m else None


def parse_error(page: str) -> str | None:
    low = page.lower()
    for marker, message in _ERROR_MARKERS:
        if marker in low:
            return message
    return None


def parse_filename(page: str, direct_url: str) -> str:
    m = _FILENAME_RE.search(page)
    if m:
        text = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if text:
            return text
    last = unquote(urlparse(direct_url).path.rstrip("/").rsplit("/", 1)[-1])
    return last or "archivo"


class MediaFireSource(Source):
    key = "mediafire"
    label = "MediaFire"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "mediafire.com" or host.endswith(".mediafire.com")

    async def inspect(self, url: str) -> list[RemoteFile]:
        if "/folder/" in urlparse(url).path:
            raise SourceError("Las carpetas de MediaFire todavía no están soportadas.")
        direct, page = await self._resolve(url)
        name = safe_filename(parse_filename(page, direct))
        size, _ = await self._probe(direct)
        return [RemoteFile(name=name, size=size, ref=url)]

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        # Se vuelve a resolver en cada intento: los enlaces directos caducan.
        direct, _ = await self._resolve(file.ref)
        headers = dict(BROWSER_HEADERS)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        async with self._http.stream("GET", direct, headers=headers) as r:
            if offset and r.status_code != 206:
                raise ResumeNotSupported("MediaFire no permite reanudar esta descarga.")
            self._check_status(r.status_code)
            async for chunk in r.aiter_bytes(READ_CHUNK):
                yield chunk

    # -- internos ---------------------------------------------------------------------

    async def _resolve(self, url: str) -> tuple[str, str]:
        if (urlparse(url).hostname or "").lower().startswith("download"):
            return url, ""  # ya es un enlace directo
        try:
            r = await self._http.get(url, headers=BROWSER_HEADERS)
        except httpx.HTTPError as exc:
            raise TransientError("No pude conectar con MediaFire.") from exc
        if r.status_code in (404, 410):
            raise SourceError("El archivo no existe o fue eliminado.")
        self._check_status(r.status_code)
        page = r.text
        direct = parse_direct_url(page)
        if not direct:
            raise SourceError(
                parse_error(page)
                or "No encontré el enlace de descarga; MediaFire pudo haber cambiado su página."
            )
        return direct, page

    async def _probe(self, direct: str) -> tuple[int | None, bool]:
        """Devuelve (tamaño, admite_rangos) pidiendo solo el primer byte."""
        headers = {**BROWSER_HEADERS, "Range": "bytes=0-0"}
        try:
            async with self._http.stream("GET", direct, headers=headers) as r:
                self._check_status(r.status_code)
                if r.status_code == 206:
                    total = r.headers.get("Content-Range", "").rpartition("/")[2]
                    return (int(total) if total.isdigit() else None), True
                length = r.headers.get("Content-Length", "")
                return (int(length) if length.isdigit() else None), False
        except httpx.HTTPError as exc:
            raise TransientError("No pude consultar el tamaño del archivo.") from exc

    @staticmethod
    def _check_status(status: int) -> None:
        if status in (403, 404, 410):
            raise SourceError("MediaFire rechazó la descarga: el enlace caducó o el archivo ya no existe.")
        if status == 429 or status >= 500:
            raise TransientError("MediaFire está limitando o fallando; reintentando.")
        if status >= 400:
            raise SourceError(f"MediaFire respondió con el error HTTP {status}.")
