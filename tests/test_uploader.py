import asyncio
import os
from dataclasses import replace

from app.config import Settings
from app.errors import UploadError
from app.sources.base import BundleFile, RemoteFile
from app.uploader import TelegramUploader


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
    )
    return replace(base, **over)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.forwarded: list[tuple] = []

    async def send_document(self, chat_id, document, **kwargs):
        self.sent.append((chat_id, document, kwargs))

    async def forward_message(self, chat_id, from_chat_id, message_id):
        self.forwarded.append((chat_id, from_chat_id, message_id))


class FakeMTProtoClient:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_messages(self, entity, ids):
        self.deleted.extend(ids)


class FakeSource:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def stream(self, remote, offset=0):
        for i in range(0, len(self.data), 1000):
            yield self.data[i : i + 1000]


class FakeMessage:
    def __init__(self, mid: int) -> None:
        self.id = mid


def test_archivo_pequeno_usa_la_bot_api():
    async def go():
        up = TelegramUploader(make_settings(bot_api_max_bytes=100), FakeBot())
        data = os.urandom(80)
        total = await up.upload(FakeSource(data), RemoteFile("f.bin", len(data), "ref"), chat_id=7)
        assert total == len(data)
        (chat_id, document, _), = up._bot.sent
        assert chat_id == 7
        assert document.filename == "f.bin"
        assert document.data == data
        assert up._bot.forwarded == []
    asyncio.run(go())


def test_archivo_grande_usa_mtproto_y_reenvia():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100, buffer_size=256), bot)
        loop = asyncio.get_running_loop()
        up._main = up._loop = loop
        up._client = FakeMTProtoClient()
        up._me_id = 999
        captured: dict[str, bytes] = {}

        async def fake_run_upload(reader, remote):
            captured["data"] = await asyncio.to_thread(reader.readall)
            return FakeMessage(55)

        up._run_upload = fake_run_upload
        data = os.urandom(5000)
        total = await up.upload(FakeSource(data), RemoteFile("big.bin", len(data), "ref"), chat_id=7)
        assert captured["data"] == data
        assert total == len(data)
        assert bot.forwarded == [(7, 999, 55)]
        assert await up.cleanup(None) == 1
        assert up._client.deleted == [55]
    asyncio.run(go())


def test_tamano_desconocido_va_a_fichero_temporal():
    """Sin tamaño conocido telethon leería el archivo entero en RAM: se escribe en disco."""
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(), bot)
        loop = asyncio.get_running_loop()
        up._main = up._loop = loop
        up._client = FakeMTProtoClient()
        up._me_id = 999
        used: list[bool] = []
        captured: dict[str, bytes] = {}

        async def fake_run_upload_path(path, remote, size):
            used.append(True)
            captured["data"] = await asyncio.to_thread(lambda: open(path, "rb").read())
            captured["size"] = size
            return FakeMessage(1)

        up._run_upload_path = fake_run_upload_path
        await up.upload(FakeSource(b"x" * 10), RemoteFile("f.bin", None, "ref"), chat_id=1)
        assert used == [True]
        assert captured["data"] == b"x" * 10
        assert captured["size"] == 10
    asyncio.run(go())


def test_zip_pequeno_va_por_la_bot_api():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=100_000), bot)
        bundle = BundleFile(
            name="carpeta.zip",
            size=10,
            ref="bundle:x",
            members=(RemoteFile("a.txt", 5, "r1", path="carpeta/a.txt"),),
        )
        total = await up.upload(FakeSource(b"datos-zip"), bundle, chat_id=7)
        assert total == len(b"datos-zip")
        (chat_id, document, _), = bot.sent
        assert chat_id == 7 and document.filename == "carpeta.zip"
    asyncio.run(go())


def test_zip_grande_se_escribe_en_disco_y_se_borra():
    async def go():
        bot = FakeBot()
        up = TelegramUploader(make_settings(bot_api_max_bytes=1, max_file_bytes=10_000_000), bot)
        loop = asyncio.get_running_loop()
        up._main = up._loop = loop
        up._client = FakeMTProtoClient()
        up._me_id = 999
        captured: dict[str, object] = {}

        async def fake_run_upload_path(path, remote, size):
            captured["path"] = path
            captured["size"] = size
            captured["data"] = await asyncio.to_thread(lambda: open(path, "rb").read())
            return FakeMessage(7)

        up._run_upload_path = fake_run_upload_path
        bundle = BundleFile(
            name="carpeta.zip",
            size=9,
            ref="bundle:x",
            members=(RemoteFile("a.txt", 9, "r1", path="carpeta/a.txt"),),
        )
        total = await up.upload(FakeSource(b"datos-zip"), bundle, chat_id=7)
        assert total == 9 and captured["size"] == 9
        assert captured["data"] == b"datos-zip"
        assert bot.forwarded == [(7, 999, 7)]
        assert not os.path.exists(str(captured["path"]))  # el temporal se borra
    asyncio.run(go())


def test_zip_demasiado_grande_se_rechaza_antes_de_empezar():
    async def go():
        up = TelegramUploader(make_settings(bot_api_max_bytes=10, max_file_bytes=100), FakeBot())
        bundle = BundleFile(name="c.zip", size=5000, ref="bundle:x", members=())
        try:
            await up.upload(FakeSource(b"x"), bundle, chat_id=1)
        except UploadError:
            return
        raise AssertionError("debía rechazar el ZIP por tamaño")
    asyncio.run(go())


def test_sin_sesion_mtproto_es_error():
    async def go():
        up = TelegramUploader(make_settings(), FakeBot())
        try:
            await up.upload(FakeSource(b"x" * 10), RemoteFile("f.bin", 999_999_999, "ref"), chat_id=1)
        except UploadError:
            return
        raise AssertionError("debía fallar sin sesión MTProto")
    asyncio.run(go())


def test_fallo_en_mtproto_no_deja_hilos_atascados_en_el_puente():
    """Si la subida falla con el buffer lleno, el productor debe poder desatascarse."""
    from app.errors import BridgeError

    async def go():
        up = TelegramUploader(make_settings(bot_api_max_bytes=100, buffer_size=1024), FakeBot())
        loop = asyncio.get_running_loop()
        up._main = up._loop = loop
        up._client = FakeMTProtoClient()
        up._me_id = 999

        async def failing_run_upload(reader, remote):
            await asyncio.sleep(0.05)  # deja que el productor llene la cola
            raise RuntimeError("explotó")

        up._run_upload = failing_run_upload
        data = os.urandom(200_000)  # >> buffer: el productor queda esperando en feed()
        try:
            await up.upload(FakeSource(data), RemoteFile("big.bin", len(data), "ref"), chat_id=7)
        except BridgeError:
            pass
        else:
            raise AssertionError("debía propagar el fallo")
        # Si feed() quedara bloqueado, el hilo seguiría vivo: comprobamos que cierra limpio.
        await asyncio.wait_for(loop.run_in_executor(None, lambda: None), timeout=5)
    asyncio.run(go())
