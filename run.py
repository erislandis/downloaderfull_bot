"""Arranque local del bot (equivale a ``python -m app.polling``).

Ejecuta desde la carpeta ``bot/``:

    python run.py
"""
from __future__ import annotations

import asyncio

from app.polling import main

if __name__ == "__main__":
    asyncio.run(main())
