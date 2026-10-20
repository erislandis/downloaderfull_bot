"""Genera la cadena de sesión MTProto (se ejecuta una sola vez, en tu PC).

Uso (desde la carpeta `bot/`):
    pip install -r requirements.txt
    python scripts/get_session.py

Lee TELEGRAM_API_ID y TELEGRAM_API_HASH del archivo `.env`, te pide el teléfono,
el código de Telegram y la contraseña 2FA (si la tienes), e imprime y GUARDA
automáticamente el TELEGRAM_SESSION en el `.env`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_PLACEHOLDER_RE = re.compile(r"^(pon_aqui|tu_api|tu_token|tu_string|xxx|change_?me)", re.IGNORECASE)


def _load_env() -> None:
    if not _ENV_FILE.is_file():
        sys.exit(f"[!] No encuentro el archivo .env en {_ENV_FILE}. Cópialo de .env.example.")
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        sys.exit("[!] Falta python-dotenv. Ejecuta: pip install -r requirements.txt")
    load_dotenv(dotenv_path=_ENV_FILE)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"').strip("'")
    if not value or _PLACEHOLDER_RE.match(value):
        sys.exit(f"[!] Rellena {name} en {_ENV_FILE} antes de continuar (sigue vacío o de ejemplo).")
    return value


def _save_session(session: str) -> None:
    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("TELEGRAM_SESSION"):
            lines[i] = "TELEGRAM_SESSION=" + session
            break
    else:
        lines.append("TELEGRAM_SESSION=" + session)
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    _load_env()
    raw_id = _required("TELEGRAM_API_ID")
    try:
        api_id = int(raw_id)
    except ValueError:
        sys.exit(f"[!] TELEGRAM_API_ID debe ser un número; ahora vale '{raw_id}'.")
    api_hash = _required("TELEGRAM_API_HASH")

    print("Conectando con Telegram… introduce teléfono, código y 2FA si lo tienes.\n")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session = client.session.save()

    print("\nTELEGRAM_SESSION=" + session)
    _save_session(session)
    print(f"\n[OK] Sesión guardada en {_ENV_FILE}. Ya puedes arrancar con:  python run.py")


if __name__ == "__main__":
    main()
