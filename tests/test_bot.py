"""El bot es público, pero los comandos de administración solo funcionan para el admin.

Se prueba el enrutado completo (router de administración + router de usuario) con un
``Bot`` de aiogram con sesión falsa: no se sale a la red en ningún momento.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Update

from app.admin import AdminRegistry
from app.bot import Services, build_dispatcher
from app.folders import FolderBrowser
from app.jobs import JobManager
from app.sources.base import RemoteFile
from tests.test_uploader import make_settings

ADMIN_ID = 4242
OTHER_ID = 777


class FakeSession(BaseSession):
    """Sesión que no sale a la red: guarda los métodos que se habrían enviado."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []

    async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
        self.requests.append(method)
        return method

    async def stream_content(self, url: str, headers: Any = None, timeout: int = 30, **kwargs: Any) -> Any:
        yield b""  # pragma: no cover


class FakeUploader:
    async def upload(self, source, remote, chat_id, on_progress=None) -> int:
        return remote.size or 0

    async def cleanup(self, max_age: float | None) -> int:
        return 0


def make_services(admin_ids: set[int] | None = None) -> tuple[Services, Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(token="42:TEST", session=session)
    settings = make_settings(admin_user_ids=frozenset(admin_ids or {ADMIN_ID}))
    jobs = JobManager(bot, settings, FakeUploader())
    services = Services(
        settings=settings,
        http=httpx.AsyncClient(),
        uploader=FakeUploader(),
        jobs=jobs,
        folders=FolderBrowser(),
        admin=AdminRegistry(settings.admin_user_ids),
        status=lambda: {
            "bot": "mi_bot",
            "uptime_seconds": 120,
            "queue": {"pending_jobs": 0, "current_file": None},
            "mtproto": {"ok": True, "error": ""},
        },
    )
    return services, bot, session


def make_update(bot: Bot, text: str, user_id: int) -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "from_user": {"id": user_id, "is_bot": False, "first_name": "U"},
                "text": text,
            },
        },
        context={"bot": bot},
    )


def sent_texts(session: FakeSession) -> list[str]:
    return [getattr(r, "text", "") for r in session.requests]


def test_el_admin_puede_usar_los_comandos_de_administracion():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/cola", ADMIN_ID))
        texts = sent_texts(session)
        assert texts, "el admin no recibió respuesta"
        assert "cola" in texts[0].lower()

    asyncio.run(go())


def test_un_usuario_normal_no_puede_usar_los_comandos_de_administracion():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/cola", OTHER_ID))
        texts = sent_texts(session)
        assert texts, "no se respondió al usuario"
        assert "🔒" in texts[0]
        # Y el comando no debe caer en el manejador de texto libre del usuario.
        assert not any("Envíame un enlace" in t for t in texts)

    asyncio.run(go())


def test_cualquiera_puede_usar_el_bot_normal():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "hola", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "Envíame un enlace" in texts[0]

    asyncio.run(go())


def test_cualquiera_puede_consultar_su_estado():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/estado", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "transferencias" in texts[0].lower()
        assert not any("🔒" in t for t in texts)

    asyncio.run(go())


def test_el_panel_de_administracion_muestra_el_estado():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/admin", ADMIN_ID))
        texts = sent_texts(session)
        assert texts
        assert "Panel de administración" in texts[0]
        assert "mi_bot" in texts[0]

    asyncio.run(go())


def test_el_router_de_administracion_va_primero():
    """Si el router de admin fuera después, sus comandos caerían en el de texto libre."""
    services, bot, _ = make_services()
    dp = build_dispatcher(services)
    assert [r.name for r in dp.sub_routers] == ["admin", "user"]


def test_limite_de_transferencias_por_usuario():
    """Un usuario normal no puede llenar la cola: como mucho 3 trabajos en cola."""
    services, bot, _ = make_services()
    source = object()
    for _ in range(3):
        services.jobs.submit(_job(OTHER_ID, source))
    assert services.jobs.pending_for(OTHER_ID) == 3
    assert services.jobs.pending_for(ADMIN_ID) == 0


def _job(user_id: int, source) -> Any:
    from app.jobs import Job

    return Job(chat_id=user_id, user_id=user_id, source=source, files=[RemoteFile("a.bin", 1, "ref")])
