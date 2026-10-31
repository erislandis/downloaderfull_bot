from __future__ import annotations

import asyncio
import base64
import html as htmllib
import re
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..errors import ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from .base import Inspection, RemoteFile, Source

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
# ``www.mediafire.com`` está detrás de Cloudflare. Para que no devuelva el reto
# «Just a moment…» hay que enviar las mismas cabeceras que un Chrome real.
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
# Los enlaces directos (``downloadNNN.mediafire.com``) no pasan por Cloudflare,
# pero no conviene pedirles HTML ni compresión.
DOWNLOAD_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "*/*",
    "Accept-Encoding": "identity",
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
_STREAM_ATTEMPTS = 2
# Cloudflare bloquea por reputación de IP, no solo por fingerprint TLS. En las IPs
# compartidas de Render el reto suele persistir: se prueban varios perfiles de Chrome
# con espera creciente y, si aun así no cede, se avisa de que no es culpa del enlace.
_IMPERSONATE_PROFILES = ("chrome", "chrome131", "safari17_0")
_CF_RETRY_DELAYS = (1.0, 2.5)
_CF_BLOCKED_MESSAGE = (
    "MediaFire está bloqueando las descargas desde la IP de este servidor "
    "(verificación de Cloudflare de datacenter). No es un fallo del enlace: "
    "reinténtalo más tarde o usa otro mirror (Mega, Drive o un enlace directo)."
)
_QUICKKEY_RE = re.compile(r"^[A-Za-z0-9]{10,25}$")
_CF_MARKERS = (
    "just a moment",
    "cf-chl",
    "cf-browser-verification",
    "checking your browser",
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
    low = href.strip().lower()
    return not low.startswith(("javascript:", "blob:", "#", "data:"))


def _valid_direct(value: str | None) -> str | None:
    """Normaliza un enlace de descarga y descarta el «repair» (no es el archivo)."""
    url = _normalize(value or "")
    if not url or _REPAIR_MARKER in url:
        return None
    return url


def _is_cloudflare_challenge(page: str) -> bool:
    """¿La respuesta es el reto anti-bot de Cloudflare en vez de la página de MediaFire?

    Ojo: la página normal de MediaFire también incluye un script de
    ``challenge-platform`` de Cloudflare, así que no sirve como señal.
    """
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

    # El botón puede venir sin id o con otro marcado: buscamos la URL en cualquier sitio.
    decoded = _valid_direct(
        _decode_scrambled(m.group(1) if (m := _SCRAMBLED_RE.search(page)) else "")
    )
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
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in ("file", "file_premium"):
        return parts[1]
    # Enlaces directos: /<dkey>/<quickkey>/<archivo>
    if host.startswith("download") and host.endswith("mediafire.com"):
        if len(parts) >= 3 and _QUICKKEY_RE.match(parts[-2]):
            return parts[-2]
        if parts and _QUICKKEY_RE.match(parts[-1]):
            return parts[-1]
    query = parsed.query.strip()
    if parsed.path.strip("/") == "" and query and "=" not in query and "&" not in query:
        return query
    # Enlaces de reparación: download_repair.php?qkey=…
    qkey = (parse_qs(parsed.query).get("qkey") or [""])[0]
    return qkey if _QUICKKEY_RE.match(qkey) else None


def canonical_page(url: str) -> str:
    """URL canónica de la página del archivo.

    Los enlaces ``downloadNNN.mediafire.com/...`` caducan y están ligados a la IP que
    abrió la página, así que nunca se usan tal cual: se vuelve a pedir la página del
    archivo para obtener un enlace directo fresco. También se quitan los parámetros
    ``?action=check&dkey=…`` que Cloudflare bloquea.
    """
    key = quickkey(url)
    if key:
        return f"https://www.mediafire.com/file/{key}/file"
    return url


def _page_title(page: str) -> str:
    m = _TITLE_RE.search(page)
    if not m:
        return ""
    return htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _is_folder_path(url: str) -> bool:
    return "/folder/" in urlparse(url).path.lower()


def _is_view_path(url: str) -> bool:
    return urlparse(url).path.lower().startswith("/view/")


@dataclass(frozen=True)
class _Page:
    """Respuesta HTML de la página del archivo (venga de httpx o de curl_cffi)."""

    status: int
    url: str
    text: str


class _RepairRequired(Exception):
    """MediaFire redirigió el enlace directo a ``download_repair.php`` (caducado)."""


class MediaFireSource(Source):
    key = "mediafire"
    label = "MediaFire"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "mediafire.com" or host.endswith(".mediafire.com")

    async def inspect(self, url: str) -> Inspection:
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
        return Inspection(files=[RemoteFile(name=name, size=size, ref=final_url)])

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
        # Se vuelve a resolver en cada intento: los enlaces directos caducan y están
        # ligados a la IP que abrió la página, así que nunca se reutilizan.
        for attempt in range(_STREAM_ATTEMPTS):
            direct, _, _, post_data = await self._resolve(file.ref)
            try:
                async for chunk in self._stream_direct(direct, post_data, offset):
                    yield chunk
                return
            except _RepairRequired:
                if attempt + 1 >= _STREAM_ATTEMPTS:
                    break
                await asyncio.sleep(1.0)
        raise SourceError(
            "MediaFire pidió reparar la descarga y no entregó el archivo. "
            "Vuelve a enviar el enlace público del archivo."
        )

    async def _stream_direct(
        self, direct: str, post_data: dict[str, str], offset: int
    ) -> AsyncIterator[bytes]:
        headers = dict(DOWNLOAD_HEADERS)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        method = "POST" if post_data else "GET"
        extra = {"data": post_data} if post_data else {}
        async with self._http.stream(method, direct, headers=headers, **extra) as r:
            if _REPAIR_MARKER in str(r.url):
                raise _RepairRequired()
            if offset and r.status_code != 206:
                raise ResumeNotSupported("MediaFire no permite reanudar esta descarga.")
            self._check_status(r.status_code)
            try:
                async for chunk in r.aiter_bytes(READ_CHUNK):
                    yield chunk
            except httpx.HTTPError as exc:
                raise TransientError("Se cortó la conexión con MediaFire; reintentando.") from exc

    # -- internos ---------------------------------------------------------------------

    async def _fetch_page(self, url: str) -> _Page:
        """Pide la página del archivo; si Cloudflare la bloquea, imita a un Chrome real."""
        try:
            r = await self._http.get(url, headers=PAGE_HEADERS)
        except httpx.HTTPError as exc:
            raise TransientError("No pude conectar con MediaFire.") from exc
        page = _Page(r.status_code, str(r.url), r.text)
        if _is_cloudflare_challenge(page.text):
            return await self._fetch_page_impersonating(url)
        return page

    @staticmethod
    async def _fetch_page_impersonating(url: str) -> _Page:
        """Cloudflare valida el fingerprint TLS de httpx; curl_cffi usa el de Chrome.

        Se prueban varios perfiles con espera creciente. Esto ayuda cuando el bloqueo
        es por fingerprint, pero **no** cambia la IP: si Cloudflare marca la IP del
        datacenter, el reto seguirá apareciendo y el llamador avisará al usuario.
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            raise SourceError(
                "MediaFire está bloqueando las descargas desde el servidor "
                "(verificación de Cloudflare). Falta la dependencia curl-cffi."
            ) from None
        headers = {"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
        last: _Page | None = None
        for attempt, profile in enumerate(_IMPERSONATE_PROFILES):
            if attempt:
                await asyncio.sleep(_CF_RETRY_DELAYS[min(attempt - 1, len(_CF_RETRY_DELAYS) - 1)])
            try:
                async with AsyncSession() as session:
                    r = await session.get(
                        url,
                        impersonate=profile,
                        headers=headers,
                        allow_redirects=True,
                        timeout=30,
                    )
            except Exception:  # noqa: BLE001  # perfil no soportado o fallo de curl_cffi
                continue
            page = _Page(int(r.status_code), str(r.url), r.text)
            if not _is_cloudflare_challenge(page.text):
                return page
            last = page
        if last is None:
            raise TransientError("No pude conectar con MediaFire.")
        return last

    async def _resolve(self, url: str) -> tuple[str, str, str, dict[str, str]]:
        page = await self._fetch_page(canonical_page(url))
        if _is_cloudflare_challenge(page.text):
            # Transitorio para que el bot reintente con espera; al agotarse, este mismo
            # mensaje explica que el problema es la IP del servidor, no el enlace.
            raise TransientError(_CF_BLOCKED_MESSAGE)
        if page.status in (404, 410):
            raise SourceError("El archivo no existe o fue eliminado.")
        self._check_status(page.status)

        final_url = page.url
        if _is_folder_path(final_url):
            raise SourceError("Es una carpeta de MediaFire; solo acepto enlaces de archivos sueltos.")
        if _is_view_path(final_url):
            raise SourceError("Ese enlace es una vista previa, no un archivo descargable.")

        html = page.text
        direct = parse_direct_url(html)
        if direct:
            # El botón normal se descarga con GET; ``pass`` solo hace falta cuando el
            # enlace lo genera la API de descarga diferida (archivos con aviso).
            return direct, html, final_url, {}
        token = parse_deferred_token(html)
        if token:
            direct = await self._deferred_download(token, final_url)
            return direct, html, final_url, parse_post_data(html)
        message = parse_error(html) or self._unexpected_page(html)
        raise SourceError(message)

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
        headers = {**DOWNLOAD_HEADERS, "Range": "bytes=0-0"}
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
