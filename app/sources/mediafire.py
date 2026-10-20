from __future__ import annotations

import asyncio
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
_DEFERRED_RE = re.compile(r"""<button\b[^>]*\bid\s*=\s*["']deferredDownloadButton["'][^>]*>""", re.I)
_SCRAMBLED_RE = re.compile(r"""data-scrambled-url\s*=\s*["']([^"']+)["']""", re.I)
_FALLBACK_RE = re.compile(
    r"""(?:https?:)?//download[^"'\s<>]*mediafire\.com/[^"'\s<>]+""", re.I
)
_JSON_URL_RE = re.compile(
    r'"(?:downloadUrl|download_url|direct_download_url)"\s*:\s*"([^"]+)"', re.I
)
_JS_HREF_RE = re.compile(
    r"""downloadButton\s*\.\s*href\s*=\s*["']([^"']+)["']""", re.I
)
_DEFERRED_CALL_RE = re.compile(
    r"""handleDeferredDownload\s*\(\s*["'][^"']*["']\s*,\s*["'][^"']*["']\s*,\s*\{(.*?)\}\s*\)""",
    re.I | re.S,
)
_JS_PROP_RE = re.compile(
    r"""([A-Za-z_$][\w$]*)\s*:\s*(?:'([^']*)'|"([^"]*)"|(-?\d+(?:\.\d+)?))"""
)
_REPAIR_MARKER = "/download_repair.php"
_FILENAME_RE = re.compile(
    r"""<div[^>]*class\s*=\s*["'][^"']*\bfilename\b[^"']*["'][^>]*>(.*?)</div>""", re.I | re.S
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DEFERRED_ENDPOINT = "https://www.mediafire.com/download_link.php"
_API_ENDPOINT = "https://www.mediafire.com/api/1.4/file/get_info.php"
_DEFERRED_RETRIES = 4
_DEFERRED_MAX_WAIT = 10.0
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
    low = href.strip().lower()
    return not low.startswith(("javascript:", "blob:", "#", "data:"))


def parse_direct_url(page: str) -> str | None:
    """Extrae el enlace directo del HTML de MediaFire (función pura, fácil de testear)."""
    m = _BUTTON_RE.search(page)
    if m:
        tag = m.group(0)
        decoded = _decode_scrambled(_attr(tag, "data-scrambled-url") or "")
        if decoded:
            return decoded
        href = _attr(tag, "href")
        if _is_usable_href(href):
            normalized = _normalize(href or "")
            if normalized:
                return normalized

    # El botón puede venir sin id o con otro marcado: buscamos la URL en cualquier sitio.
    decoded = _decode_scrambled(m.group(1) if (m := _SCRAMBLED_RE.search(page)) else "")
    if decoded:
        return decoded

    for regex in (_JSON_URL_RE, _JS_HREF_RE):
        m = regex.search(page)
        if m:
            normalized = _normalize(m.group(1))
            if normalized:
                return normalized

    m = _FALLBACK_RE.search(page)
    return _normalize(m.group(0)) if m else None


def parse_deferred_token(page: str) -> str | None:
    """Token de los archivos marcados como malware (descarga diferida por API)."""
    m = _DEFERRED_RE.search(page)
    if not m:
        return None
    return _attr(m.group(0), "data-security-token")


def parse_post_data(page: str) -> dict[str, str]:
    """Datos extra que el navegador envía por POST al bajar archivos con aviso de malware."""
    m = _DEFERRED_CALL_RE.search(page)
    if not m:
        return {}
    data: dict[str, str] = {}
    for prop in _JS_PROP_RE.finditer(m.group(1)):
        value = prop.group(2)
        if value is None:
            value = prop.group(3)
        if value is None:
            value = prop.group(4)
        if value is not None:
            data[prop.group(1)] = value
    return data


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
    last = urlparse(direct_url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(last.replace("+", " ")) or "archivo"


def quickkey(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in ("file", "file_premium"):
        return parts[1]
    query = parsed.query.strip()
    if parsed.path.strip("/") == "" and query and "=" not in query and "&" not in query:
        return query
    return None


def _page_title(page: str) -> str:
    m = _TITLE_RE.search(page)
    if not m:
        return ""
    return htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _is_folder_path(url: str) -> bool:
    return "/folder/" in urlparse(url).path.lower()


def _is_view_path(url: str) -> bool:
    return urlparse(url).path.lower().startswith("/view/")


class MediaFireSource(Source):
    key = "mediafire"
    label = "MediaFire"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "mediafire.com" or host.endswith(".mediafire.com")

    async def inspect(self, url: str) -> list[RemoteFile]:
        if _is_folder_path(url):
            raise SourceError("Las carpetas de MediaFire no están soportadas; envíame un archivo suelto.")
        direct, page, final_url, post_data = await self._resolve(url)
        name = safe_filename(parse_filename(page, direct))
        size: int | None = None
        info = await self._file_info(url) or await self._file_info(final_url)
        if info is not None:
            name, size = safe_filename(info[0]), info[1]
        if size is None:
            size, _ = await self._probe(direct, post_data)
        return [RemoteFile(name=name, size=size, ref=final_url)]

    async def _file_info(self, url: str) -> tuple[str, int] | None:
        """Nombre y tamaño exactos vía API pública de MediaFire (no siempre está disponible)."""
        key = quickkey(url)
        if not key:
            return None
        try:
            r = await self._http.get(
                _API_ENDPOINT,
                params={"quick_key": key, "response_format": "json"},
                headers=BROWSER_HEADERS,
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        response = data.get("response") or {}
        if response.get("result") != "Success":
            return None
        info = response.get("file_info") or {}
        name = info.get("filename")
        try:
            size = int(info.get("size"))
        except (TypeError, ValueError):
            return None
        return (str(name), size) if name else None

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        # Se vuelve a resolver en cada intento: los enlaces directos caducan.
        direct, _, _, post_data = await self._resolve(file.ref)
        headers = dict(BROWSER_HEADERS)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        method = "POST" if post_data else "GET"
        extra = {"data": post_data} if post_data else {}
        async with self._http.stream(method, direct, headers=headers, **extra) as r:
            if _REPAIR_MARKER in str(r.url):
                raise TransientError("MediaFire pidió reparar el enlace de descarga; reintentando.")
            if offset and r.status_code != 206:
                raise ResumeNotSupported("MediaFire no permite reanudar esta descarga.")
            self._check_status(r.status_code)
            async for chunk in r.aiter_bytes(READ_CHUNK):
                yield chunk

    # -- internos ---------------------------------------------------------------------

    async def _resolve(self, url: str) -> tuple[str, str, str, dict[str, str]]:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("download"):
            return url, "", url, {}  # ya es un enlace directo

        try:
            r = await self._http.get(url, headers=BROWSER_HEADERS)
        except httpx.HTTPError as exc:
            raise TransientError("No pude conectar con MediaFire.") from exc
        if r.status_code in (404, 410):
            raise SourceError("El archivo no existe o fue eliminado.")
        self._check_status(r.status_code)

        final_url = str(r.url)
        if _is_folder_path(final_url):
            raise SourceError("Es una carpeta de MediaFire; solo acepto enlaces de archivos sueltos.")
        if _is_view_path(final_url):
            raise SourceError("Ese enlace es una vista previa, no un archivo descargable.")

        page = r.text
        post_data = parse_post_data(page)
        direct = parse_direct_url(page)
        if not direct:
            token = parse_deferred_token(page)
            if token:
                direct = await self._deferred_download(token, final_url)
            else:
                message = parse_error(page) or self._unexpected_page(page)
                raise SourceError(message)
        return direct, page, final_url, post_data

    async def _deferred_download(self, token: str, referer: str) -> str:
        """Archivos marcados como malware: el enlace se genera con un POST."""
        headers = {
            **BROWSER_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
            "Origin": "https://www.mediafire.com",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        for attempt in range(_DEFERRED_RETRIES + 1):
            try:
                r = await self._http.post(
                    _DEFERRED_ENDPOINT, data={"security_token": token}, headers=headers
                )
            except httpx.HTTPError as exc:
                raise TransientError("No pude pedir el enlace de descarga a MediaFire.") from exc
            self._check_status(r.status_code)
            try:
                payload = r.json()
            except ValueError:
                raise SourceError("MediaFire devolvió una respuesta inesperada al generar el enlace.") from None

            if payload.get("result") == "success" and payload.get("status") == "success":
                url = _normalize(payload.get("download_url") or "")
                if url:
                    return url
                raise SourceError("MediaFire no devolvió un enlace de descarga válido.")

            if payload.get("status") == "delay" and attempt < _DEFERRED_RETRIES:
                try:
                    wait = float(payload.get("retry_after") or 3)
                except (TypeError, ValueError):
                    wait = 3.0
                await asyncio.sleep(min(max(wait, 1.0), _DEFERRED_MAX_WAIT))
                continue

            reason = payload.get("error_message") or payload.get("reason")
            raise SourceError(reason or "MediaFire no pudo generar el enlace de descarga.")

        raise TransientError("MediaFire está tardando en generar el enlace; inténtalo de nuevo.")

    @staticmethod
    def _unexpected_page(page: str) -> str:
        title = _page_title(page)
        hint = f" (página: {title})" if title else ""
        return (
            "No encontré el enlace de descarga de MediaFire"
            f"{hint}. Comprueba que el enlace es de un archivo y que sigue existiendo."
        )

    async def _probe(
        self, direct: str, post_data: dict[str, str] | None = None
    ) -> tuple[int | None, bool]:
        """Devuelve (tamaño, admite_rangos) pidiendo solo el primer byte."""
        headers = {**BROWSER_HEADERS, "Range": "bytes=0-0"}
        method = "POST" if post_data else "GET"
        extra = {"data": post_data} if post_data else {}
        try:
            async with self._http.stream(method, direct, headers=headers, **extra) as r:
                if _REPAIR_MARKER in str(r.url):
                    return None, False
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
