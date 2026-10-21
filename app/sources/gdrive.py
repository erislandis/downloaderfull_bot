"""Origen Google Drive: archivos y carpetas compartidos con «Cualquier persona con el enlace».

Dos rutas, según haya o no ``GOOGLE_API_KEY``:

* **Sin clave** (archivos sueltos): se pide el contenido a ``drive.usercontent.google.com``.
  Si Drive responde con la página «no se puede analizar en busca de virus» (archivos
  grandes), se lee su formulario y se repite la petición con los campos que pide. Esa
  página no es una API: Google puede cambiarla, y desde IPs de centro de datos a veces
  se niega a servir archivos populares.
* **Con clave** (API v3 de Drive, gratis): metadatos fiables, descarga con ``alt=media`` y,
  además, **carpetas** (se listan como árbol navegable y se pueden comprimir en ZIP).

Los documentos nativos de Google (Docs, Sheets, Slides) no tienen «archivo» que bajar: se
exportan a docx / xlsx / pptx.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from ..errors import BridgeError, QuotaExceeded, ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from ._guard import guarded_client
from ._http import filename_from_disposition, is_html, open_stream, response_size
from .archive import zip_stream
from .base import BundleFile, FolderNode, Inspection, RemoteFile, Source

READ_CHUNK = 256 * 1024
MAX_PAGE_BYTES = 1024 * 1024  # tope al leer páginas HTML (avisos de Drive, errores de la API)
MAX_TREE_NODES = 2000
MAX_TREE_DEPTH = 8

DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
API_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_GOOGLE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")

# Documentos nativos → (tipo en la URL de docs.google.com, extensión de la exportación)
_NATIVE = {
    "application/vnd.google-apps.document": ("document", "docx"),
    "application/vnd.google-apps.spreadsheet": ("spreadsheets", "xlsx"),
    "application/vnd.google-apps.presentation": ("presentation", "pptx"),
}
_EXPORT_FORMAT = {doc: fmt for doc, fmt in _NATIVE.values()}

_ID = r"[A-Za-z0-9_-]{10,}"
_FILE_PATH_RE = re.compile(rf"^/file/(?:u/\d+/)?d/({_ID})")
_FOLDER_PATH_RE = re.compile(rf"^/drive/(?:u/\d+/)?folders/({_ID})")
_DOC_PATH_RE = re.compile(rf"^/(document|spreadsheets|presentation)/(?:u/\d+/)?d/({_ID})")
_ID_RE = re.compile(rf"^{_ID}$")
_ID_QUERY_PATHS = {"/open", "/uc", "/download"}

UNRECOGNIZED = (
    "No reconozco ese enlace de Google Drive. Envía el de un archivo "
    "(Compartir → Copiar enlace) o el de una carpeta."
)
FOLDERS_NEED_KEY = (
    "Las carpetas de Google Drive necesitan una clave de API de Google "
    "(variable GOOGLE_API_KEY en el servidor). Mientras tanto, envíame el enlace de un archivo suelto."
)
NOT_PUBLIC = (
    "El archivo no es público. En Drive: Compartir → «Cualquier persona con el enlace»."
)


@dataclass(frozen=True)
class DriveLink:
    kind: str  # "file" | "folder" | "doc"
    id: str
    doc_type: str = ""  # solo para kind == "doc": document | spreadsheets | presentation


def parse_link(url: str) -> DriveLink | None:
    """Reconoce los formatos habituales de enlace de Drive (función pura, fácil de testear)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _GOOGLE_HOSTS:
        return None
    path = parsed.path
    if host == "docs.google.com" and (m := _DOC_PATH_RE.match(path)):
        return DriveLink("doc", m.group(2), m.group(1))
    if host == "drive.google.com":
        if m := _FILE_PATH_RE.match(path):
            return DriveLink("file", m.group(1))
        if m := _FOLDER_PATH_RE.match(path):
            return DriveLink("folder", m.group(1))
    query_id = (parse_qs(parsed.query).get("id") or [""])[0]
    if _ID_RE.match(query_id):
        if path == "/embeddedfolderview" and host == "drive.google.com":
            return DriveLink("folder", query_id)
        if path in _ID_QUERY_PATHS:
            return DriveLink("file", query_id)
    return None


# -- páginas HTML de Drive ---------------------------------------------------------------

_FORM_RE = re.compile(r"<form\b[^>]*>.*?</form>", re.I | re.S)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_SUBCAPTION_RE = re.compile(r'<p class="uc-error-subcaption">(.*?)</p>', re.I | re.S)

_QUOTA_MARKERS = (
    "too many users have viewed or downloaded this file",
    "download quota for this file",
    "quota exceeded",
)
_PRIVATE_MARKERS = ("you need access", "request access", "servicelogin", "accounts.google.com/signin")
_MISSING_MARKERS = (
    "the requested url was not found",
    "file you have requested does not exist",
    "no longer exists",
)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""(?<![\w-]){re.escape(name)}\s*=\s*["']([^"']*)["']""", tag, re.I)
    return m.group(1) if m else None


def parse_confirmation(page: str) -> tuple[str, dict[str, str]] | None:
    """URL y campos para continuar tras el aviso de virus de Drive (``None`` si no hay aviso)."""
    for form in _FORM_RE.finditer(page):
        block = form.group(0)
        opening = block[: block.find(">") + 1]
        action = _attr(opening, "action")
        if not action:
            continue
        action = htmllib.unescape(action)
        if "download" not in action and "/uc" not in action:
            continue
        fields: dict[str, str] = {}
        for tag in _INPUT_RE.findall(block):
            name = _attr(tag, "name")
            if name:
                fields[name] = htmllib.unescape(_attr(tag, "value") or "")
        return urljoin(DOWNLOAD_URL, action), fields
    if m := re.search(r'href="(/uc\?export=download[^"]+)"', page):
        return urljoin("https://drive.google.com", htmllib.unescape(m.group(1))), {}
    if m := re.search(r'"downloadUrl":"([^"]+)"', page):
        try:
            return json.loads(f'"{m.group(1)}"'), {}
        except ValueError:
            return m.group(1), {}
    return None


def error_from_page(page: str, status: int = 200) -> BridgeError | None:
    """Traduce una página de error de Drive a un mensaje útil (``None`` si no se reconoce)."""
    low = page.lower()
    if any(marker in low for marker in _QUOTA_MARKERS):
        return QuotaExceeded(
            "Google Drive limitó las descargas de este archivo (demasiados usuarios lo han bajado "
            "en las últimas 24 h). Prueba más tarde o haz una copia en tu propio Drive."
        )
    if status == 401 or any(marker in low for marker in _PRIVATE_MARKERS):
        return SourceError(NOT_PUBLIC)
    if status in (404, 410) or any(marker in low for marker in _MISSING_MARKERS):
        return SourceError("El archivo no existe o fue eliminado.")
    if m := _SUBCAPTION_RE.search(page):
        text = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if text:
            return SourceError(f"Google Drive dice: {text}")
    return None


async def _read_page(response: httpx.Response) -> str:
    """Lee (con tope) una respuesta HTML/JSON pequeña y la cierra."""
    try:
        data = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            data += chunk
            if len(data) >= MAX_PAGE_BYTES:
                break
        return data.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        raise TransientError("Se cortó la conexión con Google Drive; reintentando.") from exc
    finally:
        await response.aclose()


def api_error(status: int, body: str) -> BridgeError:
    """Traduce un error de la API de Drive (JSON) a un mensaje para el usuario."""
    reason = message = ""
    try:
        err = json.loads(body).get("error") or {}
        reason = ((err.get("errors") or [{}])[0] or {}).get("reason", "") or ""
        message = str(err.get("message") or "")
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    if reason == "downloadQuotaExceeded":
        return QuotaExceeded(
            "Google Drive limitó las descargas de este archivo (demasiados usuarios en 24 h). "
            "Prueba más tarde o haz una copia en tu propio Drive."
        )
    if reason == "dailyLimitExceeded":
        return QuotaExceeded("La clave de API de Google del bot agotó su cuota diaria.")
    if reason in ("rateLimitExceeded", "userRateLimitExceeded") or status == 429 or status >= 500:
        return TransientError("Google Drive está limitando las peticiones; reintentando.")
    if reason in ("keyInvalid", "accessNotConfigured") or "api key" in message.lower():
        return SourceError(
            "La clave de API de Google del bot no es válida o no tiene activada la API de Drive."
        )
    if status in (401, 403):
        return SourceError(NOT_PUBLIC)
    if status == 404:
        return SourceError("El archivo no existe o no es público (Compartir → «Cualquier persona con el enlace»).")
    return SourceError(f"Google Drive respondió con el error HTTP {status}.")


def _unique(name: str, used: dict[str, int]) -> str:
    """Drive permite nombres repetidos en una carpeta; en un ZIP/árbol deben ser únicos."""
    count = used.get(name, 0)
    used[name] = count + 1
    if not count:
        return name
    stem, dot, ext = name.rpartition(".")
    return f"{stem} ({count + 1}).{ext}" if dot and stem else f"{name} ({count + 1})"


# -- origen ------------------------------------------------------------------------------


class GoogleDriveSource(Source):
    key = "gdrive"
    label = "Google Drive"

    @property
    def _api_key(self) -> str | None:
        return self._options.google_api_key

    @classmethod
    def matches(cls, url: str) -> bool:
        return (urlparse(url).hostname or "").lower() in _GOOGLE_HOSTS

    async def inspect(self, url: str) -> Inspection:
        link = parse_link(url)
        if link is None:
            raise SourceError(UNRECOGNIZED)
        if self._api_key:
            return await self._inspect_api(link)
        if link.kind == "folder":
            raise SourceError(FOLDERS_NEED_KEY)
        async with guarded_client() as client:
            if link.kind == "doc":
                response = await self._open_export(client, link.doc_type, link.id)
                ref, fallback = f"export:{link.doc_type}:{link.id}", f"{link.id}.{_EXPORT_FORMAT[link.doc_type]}"
            else:
                response = await self._open_keyless(client, link.id, 0)
                ref, fallback = f"file:{link.id}", link.id
            try:
                name = filename_from_disposition(response.headers.get("content-disposition"))
                size = response_size(response)
            finally:
                await response.aclose()
        if size == 0:
            raise SourceError("El archivo está vacío.")
        return Inspection(files=[RemoteFile(name=safe_filename(name or fallback), size=size, ref=ref)])

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        if isinstance(file, BundleFile):
            if offset:
                raise ResumeNotSupported("Un archivo comprimido no se puede reanudar.")
            async for chunk in zip_stream(
                file.members, self._open_member, compress=self._zip_level > 0, level=self._zip_level
            ):
                yield chunk
            return

        kind, *parts = file.ref.split(":")
        async with guarded_client() as client:
            if kind == "export":
                response = await self._open_export(client, parts[0], parts[1], offset)
            elif self._api_key:
                response = await self._open_api_media(client, parts[0], offset)
            else:
                response = await self._open_keyless(client, parts[0], offset)
            try:
                if offset and response.status_code != 206:
                    raise ResumeNotSupported("Google Drive no permitió reanudar la descarga.")
                try:
                    async for chunk in response.aiter_bytes(READ_CHUNK):
                        yield chunk
                except httpx.HTTPError as exc:
                    raise TransientError("Se cortó la conexión con Google Drive; reintentando.") from exc
            finally:
                await response.aclose()

    async def _open_member(self, remote: RemoteFile) -> AsyncIterator[bytes]:
        async for chunk in self.stream(remote):
            yield chunk

    # -- sin clave ----------------------------------------------------------------------

    async def _open_keyless(self, client: httpx.AsyncClient, file_id: str, offset: int) -> httpx.Response:
        """Abre la descarga (sin leerla), saltando el aviso de virus de los archivos grandes."""
        headers = {"Range": f"bytes={offset}-"} if offset else None
        url, params = DOWNLOAD_URL, {"id": file_id, "export": "download", "confirm": "t"}
        for _ in range(2):
            response = await open_stream(client, url, headers, params)
            status = response.status_code
            if status < 400 and not is_html(response):
                return response
            if status == 429 or status >= 500:
                await response.aclose()
                raise TransientError("Google Drive está limitando las peticiones; reintentando.")
            page = await _read_page(response)
            # Primero el formulario de confirmación: si existe, es el aviso de virus (no un
            # error) y no hay que fiarse de las palabras sueltas del resto de la página.
            step = parse_confirmation(page) if status < 400 else None
            if step is None:
                raise error_from_page(page, status) or SourceError(
                    "Google Drive no entregó el archivo (pidió una verificación que no pude resolver)."
                )
            url, params = step
        raise SourceError("Google Drive pidió verificación otra vez; no pude saltarla.")

    async def _open_export(
        self, client: httpx.AsyncClient, doc_type: str, doc_id: str, offset: int = 0
    ) -> httpx.Response:
        """Exporta un documento nativo de Google (docx / xlsx / pptx)."""
        headers = {"Range": f"bytes={offset}-"} if offset else None
        url = f"https://docs.google.com/{doc_type}/d/{doc_id}/export"
        response = await open_stream(client, url, headers, {"format": _EXPORT_FORMAT[doc_type]})
        status = response.status_code
        if status < 400 and not is_html(response):
            return response
        if status == 429 or status >= 500:
            await response.aclose()
            raise TransientError("Google Drive está limitando las peticiones; reintentando.")
        page = await _read_page(response)
        raise error_from_page(page, status) or SourceError(NOT_PUBLIC)

    # -- con clave de API ---------------------------------------------------------------

    async def _api_json(self, client: httpx.AsyncClient, url: str, params: dict[str, str]) -> dict[str, Any]:
        response = await open_stream(client, url, None, {**params, "key": self._api_key or ""})
        body = await _read_page(response)
        if response.status_code >= 400:
            raise api_error(response.status_code, body)
        try:
            data = json.loads(body)
        except ValueError:
            raise SourceError("Google Drive devolvió una respuesta inesperada.") from None
        return data if isinstance(data, dict) else {}

    async def _open_api_media(self, client: httpx.AsyncClient, file_id: str, offset: int) -> httpx.Response:
        headers = {"Range": f"bytes={offset}-"} if offset else None
        params = {"alt": "media", "supportsAllDrives": "true", "key": self._api_key or ""}
        response = await open_stream(client, f"{API_URL}/{file_id}", headers, params)
        if response.status_code >= 400:
            raise api_error(response.status_code, await _read_page(response))
        return response

    async def _inspect_api(self, link: DriveLink) -> Inspection:
        fields = "id,name,size,mimeType,shortcutDetails(targetId)"
        async with guarded_client() as client:
            meta = await self._api_json(
                client, f"{API_URL}/{link.id}", {"fields": fields, "supportsAllDrives": "true"}
            )
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            if meta.get("mimeType") == SHORTCUT_MIME and target and _ID_RE.match(str(target)):
                meta = await self._api_json(
                    client, f"{API_URL}/{target}", {"fields": fields, "supportsAllDrives": "true"}
                )
            mime = str(meta.get("mimeType") or "")
            file_id = str(meta.get("id") or link.id)
            name = safe_filename(str(meta.get("name") or file_id))

            if mime == FOLDER_MIME:
                return Inspection(tree=await self._build_tree(client, file_id, name))
            if mime in _NATIVE:
                doc_type, ext = _NATIVE[mime]
                return Inspection(
                    files=[RemoteFile(f"{name}.{ext}", None, f"export:{doc_type}:{file_id}")]
                )
            if mime.startswith("application/vnd.google-apps."):
                raise SourceError("Ese tipo de archivo de Google (formulario, mapa…) no se puede descargar.")
            size = int(meta["size"]) if str(meta.get("size") or "").isdigit() else None
            if size == 0:
                raise SourceError("El archivo está vacío.")
            return Inspection(files=[RemoteFile(name, size, f"file:{file_id}")])

    async def _list_children(self, client: httpx.AsyncClient, folder_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = ""
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",  # el id ya se validó (solo [A-Za-z0-9_-])
                "fields": "nextPageToken,files(id,name,size,mimeType)",
                "pageSize": "1000",
                "orderBy": "folder,name",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data = await self._api_json(client, API_URL, params)
            items.extend(i for i in data.get("files", []) if isinstance(i, dict) and i.get("id"))
            token = str(data.get("nextPageToken") or "")
            if not token:
                return items
            if len(items) > MAX_TREE_NODES:
                return items

    async def _build_tree(self, client: httpx.AsyncClient, folder_id: str, name: str) -> FolderNode:
        count = 0

        async def walk(fid: str, fname: str, path: str, depth: int) -> FolderNode:
            nonlocal count
            node = FolderNode(name=fname, node_id=fid, is_dir=True, path=path)
            used: dict[str, int] = {}
            for item in await self._list_children(client, fid):
                count += 1
                if count > MAX_TREE_NODES:
                    raise SourceError(
                        f"La carpeta tiene demasiados elementos (máximo {MAX_TREE_NODES}). "
                        "Comparte una subcarpeta más pequeña."
                    )
                mime = str(item.get("mimeType") or "")
                child_id = str(item["id"])
                child_name = _unique(safe_filename(str(item.get("name") or child_id)), used)
                child_path = f"{path}/{child_name}" if path else child_name
                if mime == FOLDER_MIME:
                    if depth < MAX_TREE_DEPTH:
                        node.children.append(await walk(child_id, child_name, child_path, depth + 1))
                    continue
                if mime in _NATIVE:
                    doc_type, ext = _NATIVE[mime]
                    child_name = _unique(f"{child_name}.{ext}", used)
                    child_path = f"{path}/{child_name}" if path else child_name
                    ref, size = f"export:{doc_type}:{child_id}", None
                elif mime.startswith("application/vnd.google-apps."):
                    continue  # formularios, mapas…: no hay nada que descargar
                else:
                    ref = f"file:{child_id}"
                    size = int(item["size"]) if str(item.get("size") or "").isdigit() else None
                node.children.append(
                    FolderNode(name=child_name, node_id=child_id, size=size, is_dir=False, ref=ref, path=child_path)
                )
            node.size = sum(c.size or 0 for c in node.children)
            return node

        return await walk(folder_id, name, "", 0)
