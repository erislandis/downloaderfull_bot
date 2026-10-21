from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:  # python-dotenv es opcional: en Render las variables vienen del panel, no de un .env
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_REQUIRED = (
    "BOT_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
)
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{25,}$")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_PLACEHOLDER_RE = re.compile(r"^(pon_aqui|tu_api|tu_token|tu_string|xxx|change_?me)", re.IGNORECASE)
# Telegram exige que secret_token solo lleve estos caracteres (1-256)
_TELEGRAM_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_DEFAULT_BUFFER_MB = 16.0
# Trozos simultáneos de la subida por MTProto. Telegram acepta varias piezas en
# vuelo; 6 es un buen punto medio entre velocidad y riesgo de límites.
_DEFAULT_UPLOAD_PARALLELISM = 6
_MAX_UPLOAD_PARALLELISM = 8
# Nivel de compresión de los ZIP: 0 = STORE (sin comprimir). Render gratuito da
# 0,1 CPU, así que comprimir de más sale carísimo y los medios ya van comprimidos.
_DEFAULT_ZIP_LEVEL = 1


def _placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip()))


def normalize_webhook_secret(value: str) -> str:
    """Adecua un secret al formato que Telegram acepta en ``secret_token``.

    El valor que genera Render (``generateValue: true``) puede incluir caracteres
    fuera de ``[A-Za-z0-9_-]`` y Telegram rechaza el webhook con
    "secret token contains illegal characters". Si el valor ya es válido se deja
    igual; si no, se deriva de forma determinista (SHA-256 hex), de modo que el
    registro del webhook y la validación posterior usen siempre el mismo secret.
    """
    value = value.strip()
    if _TELEGRAM_SECRET_RE.match(value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    admin_user_ids: frozenset[int]
    webhook_base_url: str | None
    webhook_secret: str | None
    bot_api_max_bytes: int
    max_file_bytes: int
    buffer_size: int
    ttl_hours: float
    zip_level: int
    upload_parallelism: int
    render_deploy_hook_url: str | None
    google_api_key: str | None = None

    @property
    def source_options(self):
        """Ajustes que se pasan a los orígenes (import local: evita acoplar config y sources)."""
        from .sources.base import SourceOptions

        return SourceOptions(google_api_key=self.google_api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        if env is None:
            if load_dotenv is not None and _ENV_FILE.is_file():
                load_dotenv(dotenv_path=_ENV_FILE)
            env = os.environ

        missing = [k for k in _REQUIRED if not env.get(k) or _placeholder(env[k])]
        if missing:
            raise RuntimeError(
                "Faltan variables en el .env (o siguen con el valor de ejemplo): " + ", ".join(missing)
            )

        if not _TOKEN_RE.match(env["BOT_TOKEN"].strip()):
            raise RuntimeError("BOT_TOKEN no tiene el formato correcto (debería ser 123456:ABC...).")
        try:
            api_id = int(env["TELEGRAM_API_ID"])
        except ValueError:
            raise RuntimeError("TELEGRAM_API_ID debe ser un número (revisa tu .env).") from None
        if not _HASH_RE.match(env["TELEGRAM_API_HASH"].strip()):
            raise RuntimeError("TELEGRAM_API_HASH no tiene el formato correcto (32 caracteres hexadecimales).")
        if len(env["TELEGRAM_SESSION"].strip()) < 50:
            raise RuntimeError("TELEGRAM_SESSION parece incompleta; vuelve a generarla con scripts/get_session.py.")

        buffer = int(float(env.get("BUFFER_MB", str(_DEFAULT_BUFFER_MB))) * 1024 * 1024)
        if buffer < 64 * 1024:
            raise RuntimeError("BUFFER_MB es demasiado pequeño (mínimo 0.0625).")

        try:
            admin = frozenset(
                int(x)
                for x in (
                    env.get("ADMIN_USER_IDS", "") + "," + env.get("ALLOWED_USER_IDS", "")
                ).replace(" ", "").split(",")
                if x
            )
        except ValueError:
            raise RuntimeError(
                "ADMIN_USER_IDS debe ser una lista de números separados por comas."
            ) from None

        base = env.get("WEBHOOK_BASE_URL") or env.get("RENDER_EXTERNAL_URL")
        secret = (env.get("WEBHOOK_SECRET") or "").strip()

        try:
            zip_level = int(env.get("ZIP_LEVEL", str(_DEFAULT_ZIP_LEVEL)))
        except ValueError:
            raise RuntimeError("ZIP_LEVEL debe ser un número entero entre 0 y 9.") from None
        if not 0 <= zip_level <= 9:
            raise RuntimeError("ZIP_LEVEL debe estar entre 0 (sin comprimir) y 9.")

        try:
            parallelism = int(env.get("MT_UPLOAD_PARALLEL", str(_DEFAULT_UPLOAD_PARALLELISM)))
        except ValueError:
            raise RuntimeError("MT_UPLOAD_PARALLEL debe ser un número entero.") from None
        if parallelism < 1:
            raise RuntimeError("MT_UPLOAD_PARALLEL debe ser al menos 1.")
        parallelism = min(parallelism, _MAX_UPLOAD_PARALLELISM)

        hook = (env.get("RENDER_DEPLOY_HOOK_URL") or "").strip() or None

        return cls(
            bot_token=env["BOT_TOKEN"].strip(),
            telegram_api_id=api_id,
            telegram_api_hash=env["TELEGRAM_API_HASH"].strip(),
            telegram_session=env["TELEGRAM_SESSION"].strip(),
            admin_user_ids=admin,
            webhook_base_url=base.rstrip("/") if base else None,
            webhook_secret=normalize_webhook_secret(secret) if secret else None,
            bot_api_max_bytes=int(float(env.get("BOT_API_MAX_MB", "50")) * 1024 * 1024),
            max_file_bytes=int(float(env.get("MAX_FILE_MB", "2048")) * 1024 * 1024),
            buffer_size=buffer,
            ttl_hours=float(env.get("TTL_HOURS", "6")),
            zip_level=zip_level,
            upload_parallelism=parallelism,
            render_deploy_hook_url=hook,
            google_api_key=(env.get("GOOGLE_API_KEY") or "").strip() or None,
        )
