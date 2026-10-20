"""Reintentos ante errores transitorios en la cola de trabajos."""
import asyncio

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
