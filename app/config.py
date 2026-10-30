from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

_REQUIRED = (
    "BOT_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
)
_ALIGN = 256 * 1024  # Drive exige bloques múltiplos de 256 KiB (salvo el último)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    allowed_user_ids: frozenset[int]
    webhook_base_url: str | None
    webhook_secret: str | None
    drive_folder_name: str
    chunk_size: int
    max_file_bytes: int
    ttl_hours: float

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
        return cls(
            bot_token=env["BOT_TOKEN"],
            google_client_id=env["GOOGLE_CLIENT_ID"],
            google_client_secret=env["GOOGLE_CLIENT_SECRET"],
            google_refresh_token=env["GOOGLE_REFRESH_TOKEN"],
            allowed_user_ids=allowed,
            webhook_base_url=base.rstrip("/") if base else None,
            webhook_secret=env.get("WEBHOOK_SECRET") or None,
            drive_folder_name=env.get("DRIVE_FOLDER_NAME", "BridgeBot"),
            chunk_size=chunk,
            max_file_bytes=int(float(env.get("MAX_FILE_MB", "2048")) * 1024 * 1024),
            ttl_hours=float(env.get("TTL_HOURS", "6")),
        )
