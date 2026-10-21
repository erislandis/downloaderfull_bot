"""Administración del bot: registro de administradores e informes.

No necesita aiogram: ``app.admin`` es lógica pura a propósito.
"""
from __future__ import annotations

from app.admin import (
    ADMIN_COMMANDS,
    AdminRegistry,
    command_of,
    format_admin_panel,
    format_queue,
    format_users,
    is_admin_command,
    parse_user_id,
)


def test_registro_de_administradores():
    reg = AdminRegistry({1, 2})
    assert reg.is_admin(1) and reg.is_admin(2)
    assert not reg.is_admin(3)
    assert not reg.is_admin(None)
    assert len(reg) == 2


def test_conceder_y_revocar_permisos():
    reg = AdminRegistry({1})
    assert reg.grant(7) is True
    assert reg.grant(7) is False  # ya era admin
    assert reg.is_admin(7)
    assert reg.revoke(7) is True
    assert reg.revoke(7) is False  # ya no era admin
    assert not reg.is_admin(7)


def test_se_detectan_los_comandos_de_administracion():
    assert command_of("/admin") == "admin"
    assert command_of("/admin@mi_bot") == "admin"
    assert command_of("/ADMIN") == "admin"
    assert command_of("/cola ahora") == "cola"
    assert command_of("hola") == ""
    assert command_of("") == ""
    assert command_of(None) == ""
    assert is_admin_command("/limpiar")
    assert not is_admin_command("/estado")
    assert not is_admin_command("https://mega.nz/folder/abc#clave")


def test_se_parsean_los_ids_de_usuario():
    assert parse_user_id("/permitir 12345") == 12345
    assert parse_user_id("/revocar 12345 algo") == 12345
    assert parse_user_id("/permitir") is None
    assert parse_user_id("/permitir abc") is None
    assert parse_user_id("/permitir -3") is None
    assert parse_user_id(None) is None


def test_todos_los_comandos_de_administracion_estan_listados():
    for name in ("admin", "cola", "usuarios", "cancelar_todo", "limpiar", "broadcast", "permitir", "revocar"):
        assert name in ADMIN_COMMANDS


def test_panel_de_administracion():
    text = format_admin_panel(
        bot="mi_bot",
        uptime_seconds=3700,
        queue_pending=2,
        queue_current="carpeta.zip",
        mtproto_ok=True,
        mtproto_error="",
        users=5,
        admins=1,
        max_file_bytes=2 * 1024 * 1024 * 1024,
        sources=["MediaFire", "Mega"],
    )
    assert "mi_bot" in text
    assert "1 h 1 min" in text
    assert "2 pendiente(s)" in text
    assert "carpeta.zip" in text
    assert "✅ listo" in text
    assert "5" in text and "1" in text
    assert "Mega" in text


def test_panel_avisa_si_mtproto_falla():
    text = format_admin_panel(
        bot=None,
        uptime_seconds=10,
        queue_pending=0,
        queue_current=None,
        mtproto_ok=False,
        mtproto_error="sesión inválida",
        users=0,
        admins=1,
        max_file_bytes=1024,
        sources=[],
    )
    assert "⚠️" in text
    assert "sesión inválida" in text


def test_informe_de_la_cola():
    text = format_queue(
        [
            {"name": "uno.mp4", "size": 1024, "state": "running", "user_id": 5},
            {"name": "carpeta.zip", "size": None, "state": "queued", "user_id": 6},
        ]
    )
    assert "Cola (2)" in text
    assert "uno.mp4" in text and "subiendo" in text
    assert "carpeta.zip" in text and "en cola" in text
    assert "usuario 5" in text


def test_informe_de_la_cola_vacia():
    assert "No hay ninguna transferencia" in format_queue([])


def test_informe_de_usuarios():
    text = format_users(
        [
            {"id": 5, "jobs": 3, "ok": 2, "failed": 1},
            {"id": 6, "jobs": 1, "ok": 1, "failed": 0},
        ]
    )
    assert "Usuarios (2)" in text
    assert "3 envío(s)" in text
    assert "1 fallido(s)" in text
