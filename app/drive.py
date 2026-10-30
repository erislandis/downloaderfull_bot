"""Cliente mínimo de Google Drive v3: subida reanudable, permisos, cuota y limpieza."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .errors import DriveError, Realign, ResumeNotSupported, TransientError
from .token_store import TokenStore

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
APP_KEY = "bridgebot"  # appProperties: identifica lo creado por este bot (limpieza sin estado)


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    size: int

    @property
    def direct_link(self) -> str:
        return f"https://drive.google.com/uc?export=download&id={self.id}"


def _reasons(resp: httpx.Response) -> set[str]:
    try:
        return {e.get("reason", "") for e in resp.json()["error"].get("errors", [])}
    except Exception:  # noqa: BLE001 - cuerpo no JSON
        return set()


def _describe(resp: httpx.Response) -> str:
    if "storageQuotaExceeded" in _reasons(resp):
        return "Tu Google Drive está lleno."
    try:
        return f"{resp.json()['error']['message']} (HTTP {resp.status_code})"
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


def _persisted(resp: httpx.Response) -> int:
    """Bytes que Drive ya guardó, según la cabecera `Range: bytes=0-N`."""
    rng = resp.headers.get("Range")
    return int(rng.rpartition("-")[2]) + 1 if rng else 0


class UploadSession:
    def __init__(self, http: httpx.AsyncClient, url: str, name: str) -> None:
        self._http, self._url, self._name = http, url, name

    async def send(self, offset: int, data: bytes, last: bool, total: int | None) -> DriveFile | None:
        """Sube un bloque. Devuelve el DriveFile al completar el archivo, o None si falta más."""
        if last:
            total_s = str(offset + len(data))
        else:
            total_s = str(total) if total is not None else "*"
        content_range = f"bytes {offset}-{offset + len(data) - 1}/{total_s}" if data else f"bytes */{total_s}"
        try:
            r = await self._http.put(self._url, content=data, headers={"Content-Range": content_range})
        except httpx.HTTPError:
            raise  # lo gestiona el reintento de transfer()
        if r.status_code in (200, 201):
            return DriveFile(r.json()["id"], self._name, offset + len(data))
        if r.status_code == 308:
            persisted = _persisted(r)
            if persisted != offset + len(data):
                raise Realign(persisted)
            return None
        self._raise_for(r)
        return None  # inalcanzable

    async def query_offset(self) -> int | DriveFile:
        """Pregunta a Drive cuántos bytes tiene guardados (para reanudar tras un fallo)."""
        r = await self._http.put(self._url, content=b"", headers={"Content-Range": "bytes */*"})
        if r.status_code in (200, 201):
            body = r.json()
            return DriveFile(body["id"], self._name, int(body.get("size", 0) or 0))
        if r.status_code == 308:
            return _persisted(r)
        self._raise_for(r)
        return 0

    @staticmethod
    def _raise_for(r: httpx.Response) -> None:
        if r.status_code in (404, 410):
            raise ResumeNotSupported("La sesión de subida a Drive expiró.")
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError("Google Drive está fallando; reintentando.")
        raise DriveError(_describe(r))


class DriveClient:
    def __init__(
        self, settings: Settings, http: httpx.AsyncClient, store: TokenStore | None = None
    ) -> None:
        self._s, self._http, self._store = settings, http, store
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._folder_id: str | None = None
        self._refresh_token = (store.load() if store else None) or settings.google_refresh_token

    @property
    def has_auth(self) -> bool:
        return bool(self._refresh_token)

    def set_refresh_token(self, refresh_token: str, *, persist: bool = True) -> None:
        """Activa un token recién obtenido por el navegador y lo guarda si es posible."""
        self._refresh_token = refresh_token
        self._token = None
        self._expires_at = 0.0
        self._folder_id = None
        if persist and self._store is not None:
            try:
                self._store.save(refresh_token)
            except OSError:
                log.warning("No pude guardar el refresh token en disco", exc_info=True)

    def clear_refresh_token(self) -> None:
        self._refresh_token = None
        self._token = None
        self._expires_at = 0.0
        self._folder_id = None
        if self._store is not None:
            self._store.clear()

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            refresh_token = self._refresh_token
            if not refresh_token:
                raise DriveError(
                    "Google Drive no está conectado. Envía /conectar para autorizar la cuenta desde el navegador."
                )
            if not self._s.google_client_id or not self._s.google_client_secret:
                raise DriveError(
                    "Faltan GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET en el servidor. Añádelos y redeploya."
                )
            r = await self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._s.google_client_id,
                    "client_secret": self._s.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if r.status_code != 200:
                if "invalid_grant" in r.text:
                    self.clear_refresh_token()
                    raise DriveError(
                        "El permiso de Google Drive caducó o fue revocado. Envía /conectar para renovarlo."
                    )
                if r.status_code >= 500:
                    raise TransientError("Google no responde al renovar el acceso.")
                raise DriveError(f"No pude autenticar con Google (HTTP {r.status_code}).")
            body = r.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    async def _api(self, method: str, url: str, *, expect: tuple[int, ...] = (200,), **kwargs: Any) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        for attempt in (0, 1):
            token = await self._access_token()
            r = await self._http.request(
                method, url, headers={"Authorization": f"Bearer {token}", **extra_headers}, **kwargs
            )
            if r.status_code == 401 and attempt == 0:
                self._token = None  # token caducado: renovar y reintentar una vez
                continue
            break
        if r.status_code == 429 or r.status_code >= 500 or (
            r.status_code == 403 and _reasons(r) & {"rateLimitExceeded", "userRateLimitExceeded"}
        ):
            raise TransientError("Google Drive está limitando las peticiones.")
        if r.status_code not in expect:
            raise DriveError(_describe(r))
        return r

    # -- carpeta de trabajo ---------------------------------------------------------------

    async def ensure_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        name = self._s.drive_folder_name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"mimeType='{FOLDER_MIME}' and name='{name}' and trashed=false and 'root' in parents"
        r = await self._api("GET", f"{API}/files", params={"q": q, "fields": "files(id)", "pageSize": 1})
        files = r.json().get("files", [])
        if files:
            self._folder_id = files[0]["id"]
        else:
            r = await self._api(
                "POST", f"{API}/files", params={"fields": "id"},
                json={"name": self._s.drive_folder_name, "mimeType": FOLDER_MIME},
            )
            self._folder_id = r.json()["id"]
        return self._folder_id

    # -- subida ---------------------------------------------------------------------------

    async def start_upload(self, name: str, size: int | None, folder_id: str) -> UploadSession:
        headers = {"X-Upload-Content-Type": "application/octet-stream"}
        if size is not None:
            headers["X-Upload-Content-Length"] = str(size)
        meta = {"name": name, "parents": [folder_id], "appProperties": {APP_KEY: "1"}}
        r = await self._api(
            "POST", UPLOAD, params={"uploadType": "resumable", "fields": "id,name"}, json=meta, headers=headers
        )
        return UploadSession(self._http, r.headers["Location"], name)

    async def make_public(self, file_id: str) -> None:
        await self._api(
            "POST", f"{API}/files/{file_id}/permissions", params={"fields": "id"},
            json={"role": "reader", "type": "anyone"},
        )

    # -- mantenimiento --------------------------------------------------------------------

    async def free_bytes(self) -> int | None:
        """Espacio libre, o None si Drive no lo informa (cuenta ilimitada) o falla la consulta."""
        try:
            r = await self._api("GET", f"{API}/about", params={"fields": "storageQuota"})
            quota = r.json()["storageQuota"]
            return int(quota["limit"]) - int(quota["usage"]) if quota.get("limit") else None
        except Exception:  # noqa: BLE001 - la comprobación es opcional
            log.warning("No pude consultar la cuota de Drive", exc_info=True)
            return None

    async def cleanup(self, max_age: timedelta | None) -> int:
        """Borra (sin pasar por la papelera) los archivos del bot más viejos que `max_age`; None = todos."""
        q = f"appProperties has {{ key='{APP_KEY}' and value='1' }} and trashed=false"
        cutoff = datetime.now(timezone.utc) - max_age if max_age is not None else None
        deleted, page_token = 0, None
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,createdTime)", "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            body = (await self._api("GET", f"{API}/files", params=params)).json()
            for f in body.get("files", []):
                created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
                if cutoff is None or created < cutoff:
                    await self._api("DELETE", f"{API}/files/{f['id']}", expect=(200, 204, 404))
                    deleted += 1
            page_token = body.get("nextPageToken")
            if not page_token:
                return deleted
