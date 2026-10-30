"""Guardado del refresh token de Google fuera de las variables de entorno.

El token se guarda en un archivo local (0600) y, si Telegram está disponible,
también se puede publicar cifrado en un mensaje fijado del chat del dueño para
sobrevivir a los reinicios de Render sin necesidad de volver a autorizar.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from contextlib import suppress
from pathlib import Path

from Crypto.Cipher import AES

log = logging.getLogger(__name__)

CHAT_MARKER = "BridgeBotOAuth1:"
_FILE_VERSION = 1


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class TokenStore:
    """Persistencia local y cifrado para el refresh token de Google."""

    def __init__(
        self,
        path: str | Path,
        *,
        bot_token: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        self._path = Path(path)
        material = "\0".join((bot_token, client_id, client_secret)).encode("utf-8")
        self._key = hashlib.sha256(b"bridgebot-token-store\0" + material).digest()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> str | None:
        try:
            body = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("No pude leer el token guardado en %s", self._path, exc_info=True)
            return None
        token = body.get("refresh_token") if isinstance(body, dict) else None
        return token if isinstance(token, str) and token else None

    def save(self, refresh_token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps({"version": _FILE_VERSION, "refresh_token": refresh_token})
        tmp.write_text(payload, "utf-8")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def clear(self) -> None:
        with suppress(OSError):
            self._path.unlink()

    # -- almacenamiento cifrado en un mensaje de Telegram --------------------------------

    def encode_for_chat(self, refresh_token: str) -> str:
        nonce = os.urandom(12)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(refresh_token.encode("utf-8"))
        return CHAT_MARKER + _b64encode(nonce + tag + ciphertext)

    def decode_from_chat(self, text: str | None) -> str | None:
        if not text or not text.strip().startswith(CHAT_MARKER):
            return None
        try:
            raw = _b64decode(text.strip()[len(CHAT_MARKER):])
            nonce, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except (ValueError, KeyError):
            log.warning("El token guardado en Telegram no se pudo descifrar", exc_info=True)
            return None
