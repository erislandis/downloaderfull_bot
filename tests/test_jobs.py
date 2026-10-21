"""Reintentos ante errores transitorios en la cola de trabajos."""
import asyncio
import contextlib

from app.errors import SourceError, TransientError
from app.jobs import Job, JobManager
from app.sources.base import RemoteFile
from tests.test_uploader import make_settings


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)
        self.text = text


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = FakeMessage(text)
        self.messages.append(msg)
        return msg


class FlakyUploader:
    """Falla con TransientError las primeras ``fail_times`` llamadas."""

    def __init__(self, fail_times: int, error: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.error = error or TransientError("fallo temporal")

    async def upload(self, source, remote, chat_id, on_progress=None) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return remote.size or 0


def make_job() -> Job:
    return Job(chat_id=1, user_id=2, source=object(), files=[RemoteFile("a.bin", 10, "ref")])


def make_manager(uploader, **over) -> tuple[JobManager, FakeBot]:
    bot = FakeBot()
    mgr = JobManager(bot, make_settings(**over), uploader)
    mgr.retry_base_delay = 0  # sin esperas reales en los tests
    return mgr, bot


def test_reintenta_tras_error_transitorio_y_luego_funciona():
    async def go():
        uploader = FlakyUploader(fail_times=2)
        mgr, bot = make_manager(uploader)
        await mgr._run(make_job())
        assert uploader.calls == 3
        assert "✅" in bot.messages[0].text
    asyncio.run(go())


def test_se_rinde_tras_agotar_los_reintentos():
    async def go():
        uploader = FlakyUploader(fail_times=99)
        mgr, bot = make_manager(uploader)
        await mgr._run(make_job())
        assert uploader.calls == mgr.retry_attempts
        assert "❌" in bot.messages[0].text
    asyncio.run(go())


def test_error_no_transitorio_no_se_reintenta():
    async def go():
        uploader = FlakyUploader(fail_times=99, error=SourceError("enlace roto"))
        mgr, bot = make_manager(uploader)
        await mgr._run(make_job())
        assert uploader.calls == 1
        assert "❌" in bot.messages[0].text
    asyncio.run(go())


def test_muestra_aviso_mientras_espera_el_reintento():
    async def go():
        uploader = FlakyUploader(fail_times=1)
        mgr, bot = make_manager(uploader)
        await mgr._run(make_job())
        assert any("Reintento" in e for e in bot.messages[0].edits)
    asyncio.run(go())


# -- el trabajo sigue aunque el usuario se desconecte ---------------------------------------


class OfflineBot(FakeBot):
    """El chat del usuario no admite mensajes (se desconectó o bloqueó al bot)."""

    def __init__(self) -> None:
        super().__init__()
        self.errors = 0

    async def send_message(self, chat_id, text, **kwargs):
        self.errors += 1
        raise RuntimeError("chat not available")


def test_la_subida_sigue_aunque_no_se_pueda_avisar_al_usuario():
    """Si el chat no admite mensajes (usuario desconectado), el archivo se sube igual."""

    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        await mgr._run(make_job())
        assert uploader.calls == 1  # se subió a pesar del fallo del aviso
    asyncio.run(go())


def test_el_trabajo_se_registra_como_entregado():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        job = make_job()
        await mgr._run(job)
        assert job.state == "done"
        status = mgr.user_status(job.user_id)
        assert status[0]["name"] == "a.bin"
        assert status[0]["state"] == "done"
        assert status[0]["active"] is False
    asyncio.run(go())


def test_el_estado_muestra_los_trabajos_en_cola():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        job = make_job()
        mgr.submit(job)
        status = mgr.user_status(job.user_id)
        assert status[0]["state"] == "queued"
        assert status[0]["active"] is True
    asyncio.run(go())


def test_el_estado_es_por_usuario():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        job = make_job()
        mgr.submit(job)
        assert mgr.user_status(999) == []
    asyncio.run(go())


def test_el_historial_se_limita():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        for _ in range(10):
            job = make_job()
            await mgr._run(job)
        assert len(mgr.user_status(job.user_id)) <= 5
    asyncio.run(go())


def test_un_trabajo_entregado_no_aparece_dos_veces():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        job = make_job()
        mgr.submit(job)
        await mgr._run(job)
        status = mgr.user_status(job.user_id)
        names = [str(i["name"]) for i in status]
        assert names.count("a.bin") == 1
    asyncio.run(go())


def test_la_cola_sigue_viva_despues_de_entregar():
    """El bucle de trabajos no se detiene tras un trabajo: el siguiente entra solo."""

    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, bot = make_manager(uploader)
        task = asyncio.create_task(mgr.run_forever())
        job = make_job()
        mgr.submit(job)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if uploader.calls:
                break
        assert uploader.calls == 1
        task.cancel()
    asyncio.run(go())


def test_la_cola_se_puede_pausar_y_reanudar():
    async def go():
        mgr, _bot = make_manager(FlakyUploader(fail_times=0))
        assert not mgr.paused
        assert mgr.pause() is True
        assert mgr.paused
        assert mgr.pause() is False  # ya estaba en pausa
        assert mgr.resume() is True
        assert not mgr.paused
        assert mgr.resume() is False  # no estaba en pausa

    asyncio.run(go())


def test_las_estadisticas_acumulan_los_envios():
    async def go():
        mgr, _bot = make_manager(FlakyUploader(fail_times=0))
        await mgr._run(make_job())
        stats = mgr.stats_snapshot()
        assert stats["ok"] == 1 and stats["failed"] == 0
        assert stats["bytes"] == 10

    asyncio.run(go())


def test_las_estadisticas_cuentan_los_fallos():
    async def go():
        mgr, _bot = make_manager(FlakyUploader(fail_times=99, error=SourceError("roto")))
        await mgr._run(make_job())
        stats = mgr.stats_snapshot()
        assert stats["ok"] == 0 and stats["failed"] == 1
        assert stats["bytes"] == 0

    asyncio.run(go())


def test_la_cola_pausada_no_procesa_trabajos():
    async def go():
        uploader = FlakyUploader(fail_times=0)
        mgr, _bot = make_manager(uploader)
        mgr.pause()
        task = asyncio.create_task(mgr.run_forever())
        try:
            mgr.submit(make_job())
            await asyncio.sleep(0.05)
            assert uploader.calls == 0  # en pausa no empieza nada
            mgr.resume()
            await asyncio.sleep(0.05)
            assert uploader.calls == 1  # al reanudar arranca
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(go())
