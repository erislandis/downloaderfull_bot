"""Modo local (sin webhook): `python -m app.polling` (o `python run.py`)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

try:
    from .config import Settings
    from .runtime import Runtime
except ImportError:  # ejecutado como script suelto: `python app/polling.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import Settings
    from app.runtime import Runtime


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        print(f"\n[!] {exc}\n")
        print("Edita el archivo .env (misma carpeta que run.py) y rellena tus datos de Telegram:")
        print("  BOT_TOKEN            -> de @BotFather")
        print("  TELEGRAM_API_ID      -> https://my.telegram.org")
        print("  TELEGRAM_API_HASH    -> https://my.telegram.org")
        print("  TELEGRAM_SESSION     -> generado con: python scripts/get_session.py\n")
        raise SystemExit(1) from None
    runtime = Runtime(settings)
    await runtime.bot.delete_webhook()
    await runtime.start()
    runtime.start_background()
    try:
        await runtime.dp.start_polling(runtime.bot)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
