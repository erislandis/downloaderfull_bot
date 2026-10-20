"""Aplicación web para Render: recibe los updates de Telegram por webhook.

Además del webhook expone la página de estado en `/` y la API de estado en
`/api/status` (Render exige que el servicio sirva tráfico web).
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram.types import Update
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

try:
    from .config import Settings
    from .runtime import Runtime
except ImportError:  # ejecutado como script suelto: `python app/main.py`
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import Settings
    from app.runtime import Runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATUS_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BridgeBot · Estado</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0f1420; color: #e6ebf2;
         display: flex; min-height: 100vh; align-items: center; justify-content: center; }
  main { background: #172032; border: 1px solid #26334d; border-radius: 12px;
         padding: 2rem 2.5rem; max-width: 32rem; width: 92%; }
  h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
  .sub { color: #8fa0b8; margin: 0 0 1.25rem; }
  .badge { display: inline-block; background: #12351f; color: #63d58a; border: 1px solid #1f5a35;
           border-radius: 999px; padding: .15rem .7rem; font-size: .85rem; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: .35rem 1rem; margin: 1.25rem 0; }
  dt { color: #8fa0b8; }
  dd { margin: 0; }
  code { background: #0f1420; border-radius: 6px; padding: .1rem .4rem; }
  p { line-height: 1.5; }
  a { color: #6ab7ff; }
</style>
</head>
<body>
<main>
  <h1>BridgeBot</h1>
  <p class="sub">Puente de descargas MediaFire/Mega → Telegram</p>
  <p><span class="badge">● en línea</span></p>
  <dl>
    <dt>Bot</dt><dd>__BOT__</dd>
    <dt>Versión</dt><dd>__VERSION__</dd>
    <dt>En funcionamiento</dt><dd id="uptime">__UPTIME__</dd>
    <dt>Trabajos en cola</dt><dd id="queue">__QUEUE__</dd>
    <dt>Sitios soportados</dt><dd>__SOURCES__</dd>
    <dt>Tamaño máximo</dt><dd>__MAXSIZE__ MB</dd>
  </dl>
  <p>Envíame un enlace de <b>MediaFire</b> o <b>Mega</b> en Telegram y te devolveré el
  archivo aquí mismo como documento.</p>
  <p>API de estado: <code>GET /api/status</code></p>
</main>
<script>
async function refresh() {
  try {
    const s = await (await fetch("/api/status")).json();
    const q = s.queue || {};
    document.getElementById("queue").textContent = q.current_file
      ? q.pending_jobs + " · " + q.current_file : String(q.pending_jobs ?? 0);
    const h = Math.floor(s.uptime_seconds / 3600), m = Math.floor((s.uptime_seconds % 3600) / 60);
    document.getElementById("uptime").textContent = h ? h + " h " + m + " min" : m + " min";
  } catch (e) { /* el servicio se está despertando: se reintenta solo */ }
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def _uptime_human(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds % 60} s"
    return f"{seconds} s"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    if not settings.webhook_base_url or not settings.webhook_secret:
        raise RuntimeError("Define WEBHOOK_SECRET y WEBHOOK_BASE_URL (o RENDER_EXTERNAL_URL).")
    runtime = Runtime(settings)
    app.state.runtime = runtime
    await runtime.start()
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


@app.get("/api/status")
async def api_status(request: Request) -> dict:
    runtime: Runtime = request.app.state.runtime
    return runtime.status()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    runtime: Runtime = request.app.state.runtime
    state = runtime.status()
    return (
        STATUS_PAGE
        .replace("__BOT__", f"@{state['bot']}" if state["bot"] else "desconocido")
        .replace("__VERSION__", state["version"])
        .replace("__UPTIME__", _uptime_human(state["uptime_seconds"]))
        .replace("__QUEUE__", str(state["queue"]["pending_jobs"]))
        .replace("__SOURCES__", " · ".join(state["sources"]))
        .replace("__MAXSIZE__", str(state["max_file_mb"]))
    )


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    runtime: Runtime = request.app.state.runtime
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, runtime.settings.webhook_secret or ""):
        raise HTTPException(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
    background.add_task(runtime.dp.feed_update, runtime.bot, update)  # responde ya a Telegram
    return {"ok": True}


if __name__ == "__main__":
    raise SystemExit(
        "main.py es el servidor webhook para Render, no se ejecuta directamente.\n"
        "Para probar en tu PC usa:  python run.py"
    )
