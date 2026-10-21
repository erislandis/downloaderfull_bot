"""Administración del bot: registro de administradores e informes.

No necesita aiogram: ``app.admin`` es lógica pura a propósito.
"""
from __future__ import annotations

from app.admin import (
    ADMIN_COMMANDS,
    AdminRegistry,
    BanRegistry,
    command_of,
    format_admins,
    format_admin_panel,
    format_bans,
    format_logs,
    format_queue,
    format_stats,
    format_users,
    format_version,
    is_admin_callback,
    is_admin_command,
    parse_int,
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
    for name in (
        "admin",
        "reiniciar",
        "restart",
        "version",
        "ping",
        "stats",
        "logs",
        "test_mtproto",
        "cola",
        "usuarios",
        "pausar",
        "reanudar",
        "cancelar_todo",
        "cancelar_usuario",
        "limpiar",
        "broadcast",
        "permitir",
        "revocar",
        "admins",
        "ban",
        "unban",
        "baneados",
    ):
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


def test_se_parsea_el_numero_de_lineas():
    assert parse_int("/logs 40") == 40
    assert parse_int("/logs") is None
    assert parse_int("/logs 40", 20) == 40
    assert parse_int("/logs abc", 20) == 20


def test_se_detectan_los_callback_de_administracion():
    assert is_admin_callback("rs:yes")
    assert is_admin_callback("rs:no")
    assert not is_admin_callback("fb:abc")
    assert not is_admin_callback(None)


def test_registro_de_bloqueos():
    bans = BanRegistry()
    assert bans.ban(9, "spam") is True
    assert bans.ban(9) is False
    assert bans.is_banned(9) and not bans.is_banned(10)
    assert bans.reason(9) == "spam"
    assert bans.ids == {9}
    assert bans.unban(9) is True
    assert bans.unban(9) is False
    assert not bans.is_banned(9)
    assert bans.reason(9) == ""


def test_informe_de_estadisticas():
    text = format_stats(
        uptime_seconds=3700,
        jobs_ok=3,
        jobs_failed=1,
        bytes_sent=10 * 1024 * 1024,
        seconds_active=10.0,
        users=2,
        admins=1,
        queue_pending=0,
        queue_paused=True,
        mtproto_ok=True,
        memory_mb=48.0,
        disk_free_mb=512,
        upload_parallelism=6,
        zip_level=1,
    )
    assert "3 ok" in text and "1 fallida" in text
    assert "en pausa" in text
    assert "Memoria" in text and "48" in text
    assert "Disco libre" in text and "512" in text
    assert "Trozos simultáneos: 6" in text


def test_informe_de_registro_vacio_y_con_lineas():
    assert "No hay líneas" in format_logs([])
    text = format_logs(["hola <b>", "mundo"])
    assert "<pre>" in text and "&lt;b&gt;" in text


def test_informe_de_bloqueados():
    assert "No hay ningún usuario" in format_bans([])
    text = format_bans([{"id": 5, "reason": "spam"}])
    assert "Bloqueados (1)" in text and "5" in text and "spam" in text


def test_informe_de_administradores():
    assert "No hay ningún administrador" in format_admins([])
    text = format_admins([7, 3])
    assert "Administradores (2)" in text
    assert text.index("3") < text.index("7")


def test_informe_de_version():
    text = format_version(
        version="1.2.3",
        python="3.12.0",
        platform="Linux",
        uptime_seconds=65,
        mtproto_ok=False,
        mtproto_error="sin sesión",
    )
    assert "1.2.3" in text and "3.12.0" in text
    assert "sin sesión" in text
