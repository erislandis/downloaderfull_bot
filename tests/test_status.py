"""Página de estado (GET /) y API de estado (GET /api/status) para Render."""
import asyncio
from types import SimpleNamespace

import httpx

from app import main as main_module
from app.main import _uptime_human, app
from app.runtime import Runtime
from tests.test_uploader import make_settings


def test_runtime_status_refleja_version_y_cola():
    async def go():
        runtime = Runtime(make_settings(bot_token="123456:ABCdefGHIjklMNOpqrSTUvwxYZ123456"))
        try:
            state = runtime.status()
            assert state["ok"] is True
            assert state["status"] == "online"
            assert state["queue"] == {"pending_jobs": 0, "current_file": None, "paused": False}
            assert "MediaFire" in state["sources"] and "Mega" in state["sources"]
            assert state["uptime_seconds"] >= 0
            assert state["version"]
            assert state["admins"] == 0
        finally:
            await runtime.aclose()
    asyncio.run(go())


class StubRuntime:
    settings = SimpleNamespace(webhook_secret="secreto-de-prueba")

    def status(self) -> dict:
        return {
            "ok": True,
            "status": "online",
            "version": "9.9.9",
            "bot": "prueba_bot",
            "uptime_seconds": 7322,
            "queue": {"pending_jobs": 2, "current_file": "juego.zip"},
            "sources": ["MediaFire", "Mega"],
            "max_file_mb": 2048,
        }


def client():
    main_module.app.state.runtime = StubRuntime()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_api_status_devuelve_json():
    async def go():
        async with client() as c:
            r = await c.get("/api/status")
            assert r.status_code == 200
            data = r.json()
            assert data["bot"] == "prueba_bot"
            assert data["queue"]["current_file"] == "juego.zip"
            assert data["max_file_mb"] == 2048
    asyncio.run(go())


def test_pagina_principal_es_html_con_estado():
    async def go():
        async with client() as c:
            r = await c.get("/")
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            assert "@prueba_bot" in r.text
            assert "2 h 2 min" in r.text  # uptime 7322 s
            assert "__" not in r.text  # no quedan marcadores sin sustituir
            assert "juego.zip" not in r.text  # el detalle lo da el JS vía /api/status
    asyncio.run(go())


def test_healthz_sigue_ok():
    async def go():
        async with client() as c:
            r = await c.get("/healthz")
            assert r.status_code == 200 and r.json() == {"ok": True}
    asyncio.run(go())


def test_webhook_rechaza_secret_invalido():
    async def go():
        async with client() as c:
            r = await c.post("/webhook", json={})
            assert r.status_code == 403
    asyncio.run(go())


def test_uptime_human():
    assert _uptime_human(5) == "5 s"
    assert _uptime_human(125) == "2 min 5 s"
    assert _uptime_human(7322) == "2 h 2 min"


def test_el_webhook_se_registra_sin_tirar_el_servicio():
    """Si el registro falla (red caída) el bot arranca igual y reintenta después."""
    settings = make_settings(
        bot_token="123456:ABCdefGHIjklMNOpqrSTUvwxYZ123456",
        webhook_base_url="https://mi-bot.onrender.com",
        webhook_secret="secreto",
    )

    async def go():
        runtime = Runtime(settings)
        try:
            calls: list[dict] = []

            async def fake_set_webhook(**kwargs):
                calls.append(kwargs)
                raise ConnectionError("sin red")

            runtime.bot.set_webhook = fake_set_webhook  # type: ignore[method-assign]
            assert await runtime.ensure_webhook() is False
            assert runtime.webhook_ok is False
            assert calls[0]["url"] == "https://mi-bot.onrender.com/webhook"
            assert calls[0]["secret_token"] == "secreto"
        finally:
            await runtime.aclose()

    asyncio.run(go())


def test_el_webhook_queda_registrado_si_todo_va_bien():
    settings = make_settings(
        bot_token="123456:ABCdefGHIjklMNOpqrSTUvwxYZ123456",
        webhook_base_url="https://mi-bot.onrender.com",
        webhook_secret="secreto",
    )

    async def go():
        runtime = Runtime(settings)
        try:
            calls: list[dict] = []

            async def fake_set_webhook(**kwargs):
                calls.append(kwargs)
                return True

            runtime.bot.set_webhook = fake_set_webhook  # type: ignore[method-assign]
            assert await runtime.ensure_webhook() is True
            assert runtime.webhook_ok is True
            assert calls[0]["url"] == "https://mi-bot.onrender.com/webhook"
        finally:
            await runtime.aclose()

    asyncio.run(go())
