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
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
# www.mediafire.com está detrás de Cloudflare; imitamos las cabeceras de un Chrome real.
PAGE_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
# Los enlaces directos (downloadNNN.mediafire.com) no necesitan HTML ni compresión.
DOWNLOAD_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}
READ_CHUNK = 256 * 1024

_BUTTON_RE = re.compile(r"""<a\b[^>]*\bid\s*=\s*["']downloadButton["'][^>]*>""", re.I)
_SCRAMBLED_RE = re.compile(r"""data-scrambled-url\s*=\s*["']([^"']+)["']""", re.I)
_FALLBACK_RE = re.compile(r"""(?:https?:)?//download[^"'\s<>]*mediafire\.com/[^"'\s<>]+""", re.I)
_JSON_URL_RE = re.compile(r'"(?:downloadUrl|download_url|direct_download_url)"\s*:\s*"([^"]+)"', re.I)
_JS_HREF_RE = re.compile(r"""downloadButton\s*\.\s*href\s*=\s*["']([^"']+)["']""", re.I)
_REPAIR_MARKER = "/download_repair.php"
_FILENAME_RE = re.compile(
    r"""<div[^>]*class\s*=\s*["'][^"']*\bfilename\b[^"']*["'][^>]*>(.*?)</div>""", re.I | re.S
)
_CF_MARKERS = (
    "just a moment",
    "cf-chl",
    "cf-browser-verification",
    "checking your browser",
    "attention required",
    "you have been blocked",
    "enable javascript and cookies to continue",
)
_CF_BLOCKED_MESSAGE = (
    "MediaFire está bloqueando las descargas desde la IP de este servidor "
    "(verificación de Cloudflare de datacenter). No es un fallo del enlace. "
    "Configura MEDIAFIRE_PROXY_URL con un proxy residencial o usa Mega/Drive. "
    "También puedes abrir el enlace en tu navegador, copiar el enlace directo "
    "(download...mediafire.com) y enviármelo."
)
_ERROR_MARKERS = (
    ("dangerous file blocked", "MediaFire bloqueó este archivo por considerarlo peligroso."),
    ("file removed for violation", "El archivo fue eliminado de MediaFire por infringir sus normas."),
    ("invalid or deleted file", "El archivo no existe o fue eliminado."),
    ("something appears to be missing", "El archivo no existe, fue eliminado o el enlace está incompleto."),
    ("this file is no longer available", "El archivo ya no está disponible en MediaFire."),
    ("file has been removed", "El archivo fue eliminado de MediaFire."),
    ("belongs to a suspended account", "El archivo pertenece a una cuenta suspendida de MediaFire."),
    ("the key you provided for file download was invalid", "El enlace de MediaFire no es válido."),
    ('type="password"', "El archivo está protegido con contraseña (no soportado)."),
)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""(?<![\w-]){re.escape(name)}\s*=\s*["']([^"']*)["']""", tag, re.I)
    return m.group(1) if m else None


def _decode_scrambled(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            text = decoder(raw + "=" * (-len(raw) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            continue
        if text.startswith("http"):
            return text
    return None


def _normalize(url: str) -> str | None:
    url = htmllib.unescape(url.strip()).replace("\\/", "/")
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def _is_usable_href(href: str | None) -> bool:
    if not href:
        return False
    return not href.strip().lower().startswith(("javascript:", "blob:", "#", "data:"))


def _valid_direct(value: str | None) -> str | None:
    url = _normalize(value or "")
    if not url or _REPAIR_MARKER in url:
        return None
    return url


def _is_cloudflare_challenge(page: str) -> bool:
    """¿La respuesta es el reto anti-bot de Cloudflare en vez de la página de MediaFire?"""
    low = page.lower()
    return any(marker in low for marker in _CF_MARKERS)


def parse_direct_url(page: str) -> str | None:
    """Extrae el enlace directo del HTML de MediaFire (función pura, fácil de testear)."""
    m = _BUTTON_RE.search(page)
    if m:
        tag = m.group(0)
        decoded = _valid_direct(_decode_scrambled(_attr(tag, "data-scrambled-url") or ""))
        if decoded:
            return decoded
        href = _attr(tag, "href")
        if _is_usable_href(href):
            normalized = _valid_direct(href)
            if normalized:
                return normalized

    decoded = _valid_direct(_decode_scrambled(m.group(1) if (m := _SCRAMBLED_RE.search(page)) else ""))
    if decoded:
        return decoded

    for regex in (_JSON_URL_RE, _JS_HREF_RE):
        m = regex.search(page)
        if m:
            normalized = _valid_direct(m.group(1))
            if normalized:
                return normalized

    m = _FALLBACK_RE.search(page)
    return _valid_direct(m.group(0)) if m else None


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
    return last.replace("+", " ") or "archivo"


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
        headers = dict(DOWNLOAD_HEADERS)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        async with self._http.stream("GET", direct, headers=headers) as r:
            if _REPAIR_MARKER in str(r.url):
                raise SourceError("MediaFire pidió reparar la descarga; vuelve a enviar el enlace público.")
            if offset and r.status_code != 206:
                raise ResumeNotSupported("MediaFire no permite reanudar esta descarga.")
            self._check_status(r.status_code)
            try:
                async for chunk in r.aiter_bytes(READ_CHUNK):
                    yield chunk
            except httpx.HTTPError as exc:
                raise TransientError("Se cortó la conexión con MediaFire; reintentando.") from exc

    # -- internos ---------------------------------------------------------------------

    async def _resolve(self, url: str) -> tuple[str, str]:
        if (urlparse(url).hostname or "").lower().startswith("download"):
            return url, ""  # ya es un enlace directo
        try:
            r = await self._http.get(url, headers=PAGE_HEADERS)
        except httpx.HTTPError as exc:
            raise TransientError("No pude conectar con MediaFire.") from exc
        if r.status_code in (404, 410):
            raise SourceError("El archivo no existe o fue eliminado.")
        error = parse_error(r.text)
        if error:
            raise SourceError(error)
        if _is_cloudflare_challenge(r.text):
            raise SourceError(_CF_BLOCKED_MESSAGE)
        if r.status_code == 403:
            raise SourceError(_CF_BLOCKED_MESSAGE)
        self._check_status(r.status_code)
        page = r.text
        direct = parse_direct_url(page)
        if not direct:
            raise SourceError(
                "No encontré el enlace de descarga; MediaFire pudo haber cambiado su página "
                "o el archivo necesita una verificación que el servidor no puede resolver."
            )
        return direct, page

    async def _probe(self, direct: str) -> tuple[int | None, bool]:
        """Devuelve (tamaño, admite_rangos) pidiendo solo el primer byte."""
        headers = {**DOWNLOAD_HEADERS, "Range": "bytes=0-0"}
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
        if status == 403:
            raise SourceError(
                "MediaFire rechazó la descarga (HTTP 403): el enlace caducó o la IP del servidor "
                "está bloqueada. Prueba con MEDIAFIRE_PROXY_URL o usa otro mirror."
            )
        if status in (404, 410):
            raise SourceError("MediaFire rechazó la descarga: el enlace caducó o el archivo ya no existe.")
        if status == 429 or status >= 500:
            raise TransientError("MediaFire está limitando o fallando; reintentando.")
        if status >= 400:
            raise SourceError(f"MediaFire respondió con el error HTTP {status}.")
