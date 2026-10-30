"""Aplicación web para Render: recibe los updates de Telegram por webhook."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from html import escape
from typing import AsyncIterator

from aiogram.types import Update
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings
from .errors import BridgeError
from .runtime import Runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    if not settings.webhook_base_url or not settings.webhook_secret:
        raise RuntimeError("Define WEBHOOK_SECRET y WEBHOOK_BASE_URL (o RENDER_EXTERNAL_URL).")
    runtime = Runtime(settings)
    await runtime.bootstrap()
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


@app.get("/oauth/start")
async def oauth_start(request: Request) -> Response:
    runtime: Runtime = request.app.state.runtime
    state = request.query_params.get("state", "")
    if runtime.oauth.verify_state(state) is None:
        return HTMLResponse("<h1>Enlace no válido</h1><p>Usa /conectar en Telegram para generar uno nuevo.</p>", status_code=400)
    try:
        url = runtime.oauth.authorization_url(state)
    except BridgeError as exc:
        return HTMLResponse(f"<h1>No se puede conectar</h1><p>{escape(str(exc))}</p>", status_code=400)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    runtime: Runtime = request.app.state.runtime
    if error:
        return HTMLResponse(f"<h1>Autorización cancelada</h1><p>{escape(error)}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h1>Respuesta incompleta de Google</h1>", status_code=400)
    try:
        result = await runtime.oauth.exchange(code, state)
    except BridgeError as exc:
        return HTMLResponse(f"<h1>No se pudo conectar</h1><p>{escape(str(exc))}</p>", status_code=400)

    await runtime.persist_drive_token(result.refresh_token, result.user_id)
    try:
        await runtime.bot.send_message(
            result.user_id,
            "✅ Google Drive conectado. Ya puedes enviar enlaces y el bot subirá los archivos a tu Drive.",
        )
    except Exception:  # noqa: BLE001 - la conexión ya está hecha
        log.warning("No pude avisar por Telegram de la conexión con Drive", exc_info=True)
    return HTMLResponse(
        "<h1>Google Drive conectado</h1><p>Puedes cerrar esta pestaña y volver a Telegram.</p>"
    )
