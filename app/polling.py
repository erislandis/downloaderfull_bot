"""Modo local (sin webhook): `python -m app.polling`."""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .runtime import Runtime


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = Runtime(Settings.from_env())
    await runtime.bot.delete_webhook()
    runtime.start_background()
    try:
        await runtime.dp.start_polling(runtime.bot)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
