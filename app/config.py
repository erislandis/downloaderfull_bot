from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_REQUIRED = ("BOT_TOKEN",)
_ALIGN = 256 * 1024  # Drive exige bloques múltiplos de 256 KiB (salvo el último)
_DEFAULT_TOKEN_PATH = Path(__file__).resolve().parent.parent / ".state" / "google-token.json"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    google_client_id: str | None
    google_client_secret: str | None
    google_refresh_token: str | None
    google_redirect_uri: str | None
    allowed_user_ids: frozenset[int]
    webhook_base_url: str | None
    webhook_secret: str | None
    drive_folder_name: str
    chunk_size: int
    max_file_bytes: int
    ttl_hours: float
    token_store_path: str
    oauth_store_chat_id: int | None
    mediafire_proxy: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        missing = [k for k in _REQUIRED if not env.get(k)]
        if missing:
            raise RuntimeError("Faltan variables de entorno: " + ", ".join(missing))

        chunk = int(float(env.get("CHUNK_SIZE_MB", "8")) * 1024 * 1024)
        chunk -= chunk % _ALIGN
        if chunk < _ALIGN:
            raise RuntimeError("CHUNK_SIZE_MB es demasiado pequeño (mínimo 0.25).")

        allowed = frozenset(
            int(x) for x in env.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
        )
        base = env.get("WEBHOOK_BASE_URL") or env.get("RENDER_EXTERNAL_URL")
        base = base.rstrip("/") if base else None
        redirect = (env.get("GOOGLE_REDIRECT_URI") or "").strip() or (f"{base}/oauth/callback" if base else None)
        store_chat = (env.get("OAUTH_STORE_CHAT_ID") or "").strip()
        token_path = (env.get("TOKEN_STORE_PATH") or "").strip() or str(_DEFAULT_TOKEN_PATH)
        proxy = (env.get("MEDIAFIRE_PROXY_URL") or "").strip()
        if proxy and not proxy.lower().startswith(("http://", "https://")):
            raise RuntimeError("MEDIAFIRE_PROXY_URL debe ser una URL http:// o https://.")

        try:
            store_chat_id = int(store_chat) if store_chat else None
        except ValueError:
            raise RuntimeError("OAUTH_STORE_CHAT_ID debe ser un ID numérico de Telegram.") from None

        return cls(
            bot_token=env["BOT_TOKEN"],
            google_client_id=(env.get("GOOGLE_CLIENT_ID") or "").strip() or None,
            google_client_secret=(env.get("GOOGLE_CLIENT_SECRET") or "").strip() or None,
            google_refresh_token=(env.get("GOOGLE_REFRESH_TOKEN") or "").strip() or None,
            google_redirect_uri=redirect,
            allowed_user_ids=allowed,
            webhook_base_url=base,
            webhook_secret=env.get("WEBHOOK_SECRET") or None,
            drive_folder_name=env.get("DRIVE_FOLDER_NAME", "BridgeBot"),
            chunk_size=chunk,
            max_file_bytes=int(float(env.get("MAX_FILE_MB", "2048")) * 1024 * 1024),
            ttl_hours=float(env.get("TTL_HOURS", "6")),
            token_store_path=token_path,
            oauth_store_chat_id=store_chat_id,
            mediafire_proxy=proxy or None,
        )
