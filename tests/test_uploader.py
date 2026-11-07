"""Pruebas del uploader de la Bot API (troceado y entrega por documentos).

La suite es hermética: no hay red ni aiogram real. `FakeBot` imita a
``Bot.send_document`` y las fuentes falsas entregan bytes en memoria.
"""
import asyncio
from dataclasses import replace

import pytest

pytest.importorskip("aiogram")

from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.errors import TransientError, UploadError
from app.sources.base import RemoteFile
from app.uploader import (
    UPLOAD_SAFETY_MARGIN,
    TelegramUploader,
    _chunk_size,
    _is_too_large,
    part_filename,
)


def make_settings(**over) -> Settings:
    base = Settings(
        bot_token="token",
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session="session",
        admin_user_ids=frozenset(),
        webhook_base_url=None,
        webhook_secret=None,
        bot_api_max_bytes=50 * 1024 * 1024,
        max_file_bytes=2048 * 1024 * 1024,
        buffer_size=4 * 1024 * 1024,
        ttl_hours=6.0,
        zip_level=1,
        upload_parallelism=6,
        render_deploy_hook_url=None,
    )
    return replace(base, **over)


class FakeBot:
    """Registra los documentos enviados y puede fallar las primeras veces."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.sent: list[tuple] = []
        self.attempts = 0
        self._errors = list(errors or [])

    async def send_document(self, chat_id, document, **kwargs):
        self.attempts += 1
        if self._errors:
            raise self._errors.pop(0)
        self.sent.append((chat_id, document, kwargs))


class FakeSource:
    """Fuente en memoria; puede cortarse una vez en un desplazamiento concreto."""

    def __init__(self, data: bytes, fail_at: int | None = None) -> None:
        self.data = data
        self.fail_at = fail_at
        self.calls: list[int] = []
        self._failed = False

    async def stream(self, remote, offset=0):
        self.calls.append(offset)
        if self.fail_at is not None and not self._failed:
            self._failed = True
            for i in range(offset, min(self.fail_at, len(self.data)), 7):
                yield self.data[i : min(i + 7, self.fail_at)]
            raise TransientError("corte de prueba")
        for i in range(offset, len(self.data), 7):
            yield self.data[i : i + 7]


class TooLarge(TelegramAPIError):
    """Imita el 413 que aiogram convierte en TelegramNetworkError."""

    def __init__(self) -> None:
        self.method = "sendDocument"
        self.message = "HTTP Client says - Request Entity Too Large"
        Exception.__init__(self, self.message)


def test_chunk_size_reserva_margen_para_el_multipart():
    assert _chunk_size(50 * 1024 * 1024) == 50 * 1024 * 1024 - UPLOAD_SAFETY_MARGIN
    # Límites pequeños (tests) no se deforman.
    assert _chunk_size(100) == 100
    assert _chunk_size(8 * 1024 * 1024) == 8 * 1024 * 1024


def test_archivo_pequeno_conserva_su_nombre_y_no_lleva_pie_de_parte():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        data = b"x" * 80
        total = await up.upload(FakeSource(data), RemoteFile("f.bin", len(data), "ref"), chat_id=7)

        assert total == len(data)
        (chat_id, document, kwargs), = bot.sent
        assert chat_id == 7
        assert document.filename == "f.bin"
        assert document.data == data
        assert "caption" not in kwargs
        assert up.last_parts == 1

    asyncio.run(go())


def test_archivo_grande_se_trocea_con_nombres_partNNN_y_pie_de_parte():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        data = b"a" * 250
        total = await up.upload(FakeSource(data), RemoteFile("big.bin", len(data), "ref"), chat_id=7)

        assert total == len(data)
        assert [d.filename for _, d, _ in bot.sent] == [
            "big.part001.bin",
            "big.part002.bin",
            "big.part003.bin",
        ]
        assert [len(d.data) for _, d, _ in bot.sent] == [100, 100, 50]
        assert all(kwargs.get("caption", "").startswith("big.bin") for _, _, kwargs in bot.sent)
        assert up.last_parts == 3

    asyncio.run(go())


def test_multiplo_exacto_del_tamano_de_parte_no_deja_una_parte_vacia():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        data = b"b" * 200
        await up.upload(FakeSource(data), RemoteFile("big.bin", len(data), "ref"), chat_id=1)

        assert [len(d.data) for _, d, _ in bot.sent] == [100, 100]
        assert [d.filename for _, d, _ in bot.sent] == ["big.part001.bin", "big.part002.bin"]

    asyncio.run(go())


def test_archivo_mayor_que_el_maximo_se_rechaza_antes_de_enviar():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100, max_file_bytes=50), bot)
        with pytest.raises(UploadError):
            await up.upload(FakeSource(b"x"), RemoteFile("f.bin", 100, "ref"), chat_id=1)
        assert bot.sent == []

    asyncio.run(go())


def test_archivo_vacio_se_rechaza():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        with pytest.raises(UploadError, match="vacío"):
            await up.upload(FakeSource(b""), RemoteFile("f.bin", 0, "ref"), chat_id=1)
        assert bot.sent == []

    asyncio.run(go())


def test_413_falla_con_mensaje_claro_y_sin_reintentar():
    async def go():
        bot = FakeBot(errors=[TooLarge()])
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        with pytest.raises(UploadError, match="413"):
            await up.upload(FakeSource(b"x" * 80), RemoteFile("f.bin", 80, "ref"), chat_id=1)
        assert bot.attempts == 1
        assert bot.sent == []

    asyncio.run(go())


def test_error_transitorio_reanuda_desde_el_ultimo_byte(monkeypatch):
    async def go():
        monkeypatch.setattr("app.uploader.DOWNLOAD_BASE_DELAY", 0.0)
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        data = b"c" * 250
        source = FakeSource(data, fail_at=70)

        total = await up.upload(source, RemoteFile("big.bin", len(data), "ref"), chat_id=1)

        assert total == len(data)
        assert source.calls == [0, 70]
        assert b"".join(d.data for _, d, _ in bot.sent) == data

    asyncio.run(go())


def test_progreso_informa_del_total_descargado():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), bot)
        data = b"d" * 250
        seen: list[int] = []

        async def progress(n: int) -> None:
            seen.append(n)

        await up.upload(
            FakeSource(data), RemoteFile("big.bin", len(data), "ref"), chat_id=1, on_progress=progress
        )

        assert seen
        assert seen[-1] == len(data)

    asyncio.run(go())


def test_part_filename_conserva_la_extension_y_limita_el_tallo():
    assert part_filename("video.mp4", 1) == "video.part001.mp4"
    assert part_filename("sin_extension", 12) == "sin_extension.part012"
    assert part_filename("x" * 300 + ".bin", 1).endswith(".part001.bin")


def test_is_too_large_reconoce_el_413_y_no_otros_errores():
    assert _is_too_large(TooLarge())
    assert _is_too_large(RuntimeError("Request Entity Too Large"))
    assert _is_too_large(RuntimeError('{"error_code":413,"description":"..."}'))
    assert not _is_too_large(RuntimeError("Bad Request: chat not found"))
    # Un límite de ritmo con 413 segundos no es un 413 de tamaño.
    assert not _is_too_large(RuntimeError("Too Many Requests: retry after 413"))
