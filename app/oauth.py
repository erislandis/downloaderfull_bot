"""OAuth 2.0 de Google iniciado desde el navegador (sin token en variables de entorno)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .drive import DriveClient
from .errors import DriveError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
STATE_TTL = 15 * 60


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@dataclass(frozen=True)
class OAuthResult:
    refresh_token: str
    user_id: int


class OAuthService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, drive: DriveClient) -> None:
        self._s, self._http, self._drive = settings, http, drive

    @property
    def configured(self) -> bool:
        return bool(self._s.google_client_id and self._s.google_client_secret and self._s.google_redirect_uri)

    def _secret(self) -> bytes:
        material = "\0".join(
            (
                self._s.webhook_secret or "",
                self._s.google_client_id or "",
                self._s.google_client_secret or "",
                self._s.bot_token,
            )
        )
        return hashlib.sha256(b"bridgebot-oauth-state\0" + material.encode("utf-8")).digest()

    def make_state(self, user_id: int) -> str:
        payload = {
            "uid": user_id,
            "exp": int(time.time()) + STATE_TTL,
            "nonce": secrets.token_urlsafe(12),
        }
        raw = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self._secret(), raw.encode("ascii"), hashlib.sha256).digest())
        return f"{raw}.{signature}"

    def verify_state(self, state: str) -> dict[str, Any] | None:
        try:
            raw, signature = state.split(".", 1)
            expected = _b64encode(hmac.new(self._secret(), raw.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_b64decode(raw))
            if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
                return None
            if not isinstance(payload.get("uid"), int):
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def start_url(self, user_id: int) -> str:
        if not self.configured:
            raise DriveError(
                "Falta configurar GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET o la URL de retorno de Google."
            )
        if not self._s.webhook_base_url:
            raise DriveError("Define WEBHOOK_BASE_URL (o usa Render, que define RENDER_EXTERNAL_URL).")
        return f"{self._s.webhook_base_url}/oauth/start?state={self.make_state(user_id)}"

    def authorization_url(self, state: str) -> str:
        if not self._s.google_client_id or not self._s.google_redirect_uri:
            raise DriveError("Falta configurar el cliente OAuth de Google.")
        params = {
            "client_id": self._s.google_client_id,
            "redirect_uri": self._s.google_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def exchange(self, code: str, state: str) -> OAuthResult:
        payload = self.verify_state(state)
        if payload is None:
            raise DriveError("El enlace de conexión caducó o no es válido. Usa /conectar de nuevo.")
        if not self._s.google_client_id or not self._s.google_client_secret or not self._s.google_redirect_uri:
            raise DriveError("El cliente OAuth de Google no está configurado en el servidor.")

        r = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._s.google_client_id,
                "client_secret": self._s.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self._s.google_redirect_uri,
            },
        )
        if r.status_code != 200:
            if r.status_code >= 500:
                raise DriveError("Google no responde ahora mismo; inténtalo de nuevo.")
            raise DriveError("Google rechazó la autorización. Revisa la configuración del cliente OAuth.")
        body = r.json()
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            raise DriveError(
                "Google no devolvió un refresh token. Vuelve a intentarlo desde /conectar y acepta el acceso."
            )
        self._drive.set_refresh_token(str(refresh_token))
        return OAuthResult(str(refresh_token), int(payload["uid"]))
