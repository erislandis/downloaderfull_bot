"""El bot de punta a punta con los nuevos orígenes: mensaje con enlace → análisis → cola."""
import asyncio

import httpx
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Message

from app.bot import build_dispatcher
from tests import test_bot
from tests.test_direct_gdrive import FILE_ID, FOLDER_ID, patch_clients
from tests.test_bot import make_services, make_update

USER = 4242


class MessageSession(test_bot.FakeSession):
    """Como FakeSession, pero devuelve un Message para poder editar el mensaje de estado."""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message.model_validate(
                {"message_id": len(self.requests), "date": 1700000000,
                 "chat": {"id": USER, "type": "private"}, "text": method.text},
                context={"bot": bot},
            )
        return method


def send(monkeypatch, text):
    """Manda `text` al bot y devuelve (textos enviados, servicios)."""
    monkeypatch.setattr(test_bot, "FakeSession", MessageSession)

    async def go():
        services, bot, session = make_services()
        await build_dispatcher(services).feed_update(bot, make_update(bot, text, USER))
        return [getattr(r, "text", "") for r in session.requests], services

    return asyncio.run(go())


def test_enlace_directo_entra_en_la_cola(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=b"x" * 5000, headers={"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="juego.zip"'}))
    texts, services = send(monkeypatch, "mira esto https://cdn.example.com/dl/123")
    assert any("Analizando enlace directo" in t for t in texts)
    assert any("juego.zip" in t and "En cola" in t for t in texts)
    assert services.jobs.pending_for(USER) == 1


def test_pagina_web_se_rechaza_con_mensaje_claro(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, text="<html></html>", headers={"Content-Type": "text/html"}))
    texts, services = send(monkeypatch, "https://blog.example.com/articulo")
    assert any("página web" in t for t in texts)
    assert services.jobs.pending_for(USER) == 0


def test_destino_interno_se_bloquea(monkeypatch):
    texts, services = send(monkeypatch, "http://127.0.0.1:10000/api/status")
    assert any("interna" in t for t in texts)
    assert services.jobs.pending_for(USER) == 0


def test_carpeta_de_drive_sin_clave_explica_que_falta(monkeypatch):
    texts, services = send(monkeypatch, f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    assert any("GOOGLE_API_KEY" in t for t in texts)
    assert services.jobs.pending_for(USER) == 0


def test_archivo_de_drive_entra_en_la_cola(monkeypatch):
    patch_clients(monkeypatch, lambda req: httpx.Response(
        200, content=b"y" * 4000,
        headers={"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename*=UTF-8''Tesis%20final.pdf"}))
    texts, services = send(monkeypatch, f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing")
    assert any("Tesis final.pdf" in t and "En cola" in t for t in texts)
    assert services.jobs.pending_for(USER) == 1
