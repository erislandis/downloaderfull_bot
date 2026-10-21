"""Tests del empaquetador ZIP en streaming."""
import asyncio
import io
import zipfile

import pytest

from app.sources.archive import ArchiveError, member_name, zip_stream
from app.sources.base import RemoteFile


async def _collect(members, opener=None, compress=True) -> bytes:
    if opener is None:
        async def opener(remote):
            yield remote.name.encode()
            yield b"contenido de " + remote.name.encode()
    return b"".join([c async for c in zip_stream(members, opener, compress=compress)])


def _members():
    return [
        RemoteFile("a.txt", 10, "r1", path="Carpeta/a.txt"),
        RemoteFile("b.bin", 20, "r2", path="Carpeta/sub/b.bin"),
    ]


def test_zip_valido_para_zipfile():
    data = asyncio.run(_collect(_members()))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    assert z.namelist() == ["Carpeta/a.txt", "Carpeta/sub/b.bin"]
    assert z.read("Carpeta/a.txt") == b"a.txtcontenido de a.txt"
    assert z.read("Carpeta/sub/b.bin") == b"b.bincontenido de b.bin"


def test_zip_sin_compresion():
    data = asyncio.run(_collect(_members(), compress=False))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    assert z.getinfo("Carpeta/a.txt").compress_type == zipfile.ZIP_STORED
    assert z.read("Carpeta/a.txt") == b"a.txtcontenido de a.txt"


def test_zip_con_un_solo_miembro():
    data = asyncio.run(_collect([RemoteFile("solo.txt", 4, "r", path="solo.txt")]))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == ["solo.txt"]
    assert z.testzip() is None


def test_zip_vacio():
    data = asyncio.run(_collect([]))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == []


def test_rutas_inseguras_se_neutralizan():
    assert member_name(RemoteFile("x", 1, "r", path="../../etc/passwd")) == "etc/passwd"
    assert member_name(RemoteFile("x", 1, "r", path="/absoluto.txt")) == "absoluto.txt"
    assert member_name(RemoteFile("x", 1, "r", path="")) == "x"


def test_demasiados_miembros():
    members = [RemoteFile(f"f{i}.txt", 1, "r") for i in range(70_000)]
    with pytest.raises(ArchiveError):
        asyncio.run(_collect(members))
