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

from app.admin import AdminRegistry, BanRegistry
from app.bot import Services, build_dispatcher
from app.folders import FolderBrowser
from app.jobs import JobManager
from app.sources.base import RemoteFile
from tests.test_uploader import make_settings

ADMIN_ID = 4242
OTHER_ID = 777


async def noop_restart() -> tuple[str, str]:
    return "deploy", ""


async def noop_healthcheck() -> tuple[bool, str]:
    return True, ""


async def noop_refresh() -> None:
    return None


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
    settings = make_settings(admin_user_ids=frozenset({ADMIN_ID} if admin_ids is None else admin_ids))
    jobs = JobManager(bot, settings, FakeUploader())
    services = Services(
        settings=settings,
        http=httpx.AsyncClient(),
        uploader=FakeUploader(),
        jobs=jobs,
        folders=FolderBrowser(),
        admin=AdminRegistry(settings.admin_user_ids),
        bans=BanRegistry(),
        status=lambda: {
            "bot": "mi_bot",
            "uptime_seconds": 120,
            "queue": {"pending_jobs": 0, "current_file": None},
            "mtproto": {"ok": True, "error": ""},
        },
        logs=lambda count: [],
        resources=lambda: {"memory_mb": 32.0, "disk_free_mb": 1024},
        restart=noop_restart,
        healthcheck=noop_healthcheck,
        refresh_commands=noop_refresh,
        environment=lambda: {
            "version": "1.1.0",
            "python": "3.12",
            "platform": "Linux",
            "uptime_seconds": 120,
            "mtproto_ok": True,
            "mtproto_error": "",
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


def make_callback(bot: Bot, data: str, user_id: int) -> Update:
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-1",
                "from_user": {"id": user_id, "is_bot": False, "first_name": "U"},
                "chat_instance": "1",
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": 1700000000,
                    "chat": {"id": user_id, "type": "private"},
                    "from_user": {"id": bot.id, "is_bot": True, "first_name": "B"},
                    "text": "…",
                },
            },
        },
        context={"bot": bot},
    )


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


def test_el_reinicio_pide_confirmacion():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/reiniciar", ADMIN_ID))
        texts = sent_texts(session)
        assert any("¿Reiniciar" in t for t in texts)
        assert any("rs:yes" == getattr(b, "callback_data", "") for r in session.requests
                   for b in (getattr(getattr(r, "reply_markup", None), "inline_keyboard", None) or [[]])[0])

    asyncio.run(go())


def test_solo_el_admin_puede_confirmar_el_reinicio():
    async def go():
        calls: list[bool] = []

        async def fake_restart() -> tuple[str, str]:
            calls.append(True)
            return "deploy", ""

        services, bot, session = make_services()
        services.restart = fake_restart
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_callback(bot, "rs:yes", OTHER_ID))
        assert calls == []  # un usuario normal no reinicia el bot
        assert any("administradores" in t for t in sent_texts(session))

        await dp.feed_update(bot, make_callback(bot, "rs:yes", ADMIN_ID))
        assert calls == [True]
        assert any("Redeploy" in t for t in sent_texts(session))

    asyncio.run(go())


def test_el_reinicio_se_puede_cancelar():
    async def go():
        calls: list[bool] = []

        async def fake_restart() -> tuple[str, str]:
            calls.append(True)
            return "deploy", ""

        services, bot, session = make_services()
        services.restart = fake_restart
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_callback(bot, "rs:no", ADMIN_ID))
        assert calls == []
        assert any("cancelado" in t.lower() for t in sent_texts(session))

    asyncio.run(go())


def test_un_usuario_bloqueado_no_puede_usar_el_bot():
    async def go():
        services, bot, session = make_services()
        services.bans.ban(OTHER_ID, "spam")
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "hola", OTHER_ID))
        texts = sent_texts(session)
        assert texts and "bloqueado" in texts[0]
        assert not any("Envíame un enlace" in t for t in texts)

    asyncio.run(go())


def test_help_es_alias_de_ayuda():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/help", OTHER_ID))
        texts = sent_texts(session)
        assert texts and "Envíame un enlace" in texts[0]

    asyncio.run(go())


def test_el_admin_tiene_los_nuevos_comandos():
    """Cada comando nuevo responde al administrador (y no con el aviso de permisos)."""
    expected = {
        "/version": "Versión",
        "/ping": "Pong",
        "/stats": "Estadísticas",
        "/logs": "líneas",
        "/test_mtproto": "MTProto",
        "/pausar": "pausa",
        "/reanudar": "reanudada",
        "/admins": "Administradores",
        "/baneados": "bloqueado",
        "/ban 999": "bloqueado",
        "/unban 999": "no estaba",
        "/cancelar_usuario 999": "no tenía",
    }

    async def go():
        for command, fragment in expected.items():
            services, bot, session = make_services()
            dp = build_dispatcher(services)
            await dp.feed_update(bot, make_update(bot, command, ADMIN_ID))
            texts = " ".join(sent_texts(session))
            assert fragment.lower() in texts.lower(), f"{command} no respondió: {texts!r}"
            assert "🔒" not in texts, f"{command} fue bloqueado"

    asyncio.run(go())


def test_al_iniciar_el_admin_ve_sus_comandos_y_opciones():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/start", ADMIN_ID))
        texts = sent_texts(session)
        assert texts
        assert "Panel de administración" in texts[0]
        assert "/reiniciar" in texts[0] and "/pausar" in texts[0]
        assert f"<code>{ADMIN_ID}</code>" in texts[0]

    asyncio.run(go())


def test_al_iniciar_un_usuario_normal_ve_las_opciones_normales():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/start", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "Envíame un enlace" in texts[0]
        assert "Panel de administración" not in texts[0]
        assert "/reiniciar" not in texts[0]
        assert f"<code>{OTHER_ID}</code>" in texts[0]

    asyncio.run(go())


def test_sin_admins_configurados_el_inicio_avisa_como_arreglarlo():
    async def go():
        services, bot, session = make_services(admin_ids=set())
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/start", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "No hay administradores configurados" in texts[0]
        assert "ADMIN_USER_IDS" in texts[0]
        assert f"<code>{OTHER_ID}</code>" in texts[0]

    asyncio.run(go())


def test_el_aviso_de_permisos_muestra_el_id_del_usuario():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/admin", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "ADMIN_USER_IDS" in texts[0]
        assert f"<code>{OTHER_ID}</code>" in texts[0]

    asyncio.run(go())


def test_si_el_hook_de_reinicio_falla_el_bot_reinicia_igual():
    async def go():
        async def boom() -> tuple[str, str]:
            raise RuntimeError("deploy hook roto")

        services, bot, session = make_services()
        services.restart = boom
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_callback(bot, "rs:yes", ADMIN_ID))
        texts = sent_texts(session)
        assert any("Reiniciando el proceso" in t for t in texts)

    asyncio.run(go())


def test_un_comando_desconocido_no_se_confunde_con_un_enlace():
    async def go():
        services, bot, session = make_services()
        dp = build_dispatcher(services)
        await dp.feed_update(bot, make_update(bot, "/comando_que_no_existe", OTHER_ID))
        texts = sent_texts(session)
        assert texts
        assert "No conozco ese comando" in texts[0]
        assert "Envíame un enlace" not in texts[0]

    asyncio.run(go())
