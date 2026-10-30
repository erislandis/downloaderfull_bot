"""Aplicación web para Render: recibe los updates de Telegram por webhook."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram.types import Update
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .config import Settings
from .runtime import Runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    if not settings.webhook_base_url or not settings.webhook_secret:
        raise RuntimeError("Define WEBHOOK_SECRET y WEBHOOK_BASE_URL (o RENDER_EXTERNAL_URL).")
    runtime = Runtime(settings)
    app.state.runtime = runtime
    await runtime.bot.set_webhook(
        url=f"{settings.webhook_base_url}/webhook",
        secret_token=settings.webhook_secret,
        allowed_updates=runtime.dp.resolve_used_update_types(),
    )
    runtime.start_background()
    try:
        yield
    finally:
        await runtime.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    runtime: Runtime = request.app.state.runtime
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, runtime.settings.webhook_secret or ""):
        raise HTTPException(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
    background.add_task(runtime.dp.feed_update, runtime.bot, update)  # responde ya a Telegram
    return {"ok": True}
