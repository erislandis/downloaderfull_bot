import asyncio
import os

from app.drive import DriveFile
from app.errors import ResumeNotSupported, TransientError
from app.sources.base import RemoteFile
from app.transfer import rechunk, transfer

CHUNK = 4096


async def nosleep(_):
    return None


async def collect(gen):
    return [item async for item in gen]


async def feed(parts):
    for p in parts:
        yield p


class FakeSession:
    def __init__(self):
        self.buf = bytearray()

    async def send(self, offset, data, last, total):
        assert offset == len(self.buf), "el bloque no es contiguo"
        self.buf += data
        return DriveFile("id1", "n", len(self.buf)) if last else None

    async def query_offset(self):
        return len(self.buf)


class FakeDrive:
    def __init__(self):
        self.sessions = []

    async def start_upload(self, name, size, folder_id):
        self.sessions.append(FakeSession())
        return self.sessions[-1]


class FakeSource:
    def __init__(self, data, fail_at=None, error=TransientError("boom")):
        self.data, self.fail_at, self.error, self.failed = data, fail_at, error, False
        self.offsets = []

    async def stream(self, remote, offset=0):
        self.offsets.append(offset)
        for i in range(offset, len(self.data), 1000):
            if self.fail_at is not None and i >= self.fail_at and not self.failed:
                self.failed = True
                raise self.error
            yield self.data[i:i + 1000]


def run(data, size, **source_kwargs):
    source, drive = FakeSource(data, **source_kwargs), FakeDrive()
    remote = RemoteFile("f.bin", size, "ref")
    result = asyncio.run(transfer(source, remote, drive, "folder", chunk_size=CHUNK, sleep=nosleep))
    return source, drive, result


def test_rechunk_marca_el_ultimo_bloque():
    out = asyncio.run(collect(rechunk(feed([b"a" * 5000, b"b" * 5000]), CHUNK)))
    assert [len(c) for c, _ in out] == [CHUNK, CHUNK, 10000 - 2 * CHUNK]
    assert [last for _, last in out] == [False, False, True]


def test_rechunk_multiplo_exacto_y_vacio():
    out = asyncio.run(collect(rechunk(feed([b"x" * (CHUNK * 2)]), CHUNK)))
    assert [last for _, last in out] == [False, True] and all(len(c) == CHUNK for c, _ in out)
    assert asyncio.run(collect(rechunk(feed([]), CHUNK))) == [(b"", True)]


def test_transferencia_normal_con_tamano_conocido_y_desconocido():
    data = os.urandom(10_000)
    for size in (len(data), None):
        _, drive, result = run(data, size)
        assert bytes(drive.sessions[0].buf) == data and result.size == len(data)


def test_reanuda_desde_lo_confirmado_tras_fallo_temporal():
    data = os.urandom(10_000)
    source, drive, _ = run(data, len(data), fail_at=9000)
    assert bytes(drive.sessions[0].buf) == data
    # El primer bloque (4096) ya estaba confirmado: se reanuda desde ahí, no desde cero.
    assert source.offsets == [0, CHUNK]


def test_reinicia_con_nueva_sesion_si_no_se_puede_reanudar():
    data = os.urandom(10_000)
    source, drive, _ = run(data, len(data), fail_at=6000, error=ResumeNotSupported("no"))
    assert len(drive.sessions) == 2 and bytes(drive.sessions[1].buf) == data
    assert source.offsets[-1] == 0


def test_archivo_vacio():
    _, drive, result = run(b"", 0)
    assert result.size == 0 and bytes(drive.sessions[0].buf) == b""
