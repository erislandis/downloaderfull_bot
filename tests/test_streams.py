import threading
import time

import pytest

from app.streams import StreamReader


def test_lee_bloques_y_sirve_lo_pedido():
    reader = StreamReader(1024)
    reader.feed(b"a" * 10)
    reader.feed(b"b" * 5)
    reader.finish()
    assert reader.read(4) == b"aaaa"
    assert reader.read(100) == b"aaaaaa" + b"bbbbb"
    assert reader.read(4) == b""


def test_readall_junta_todo():
    reader = StreamReader(1024)
    reader.feed(b"123")
    reader.feed(b"456")
    reader.finish()
    assert reader.readall() == b"123456"


def test_error_se_propaga_al_lector():
    reader = StreamReader(1024)
    reader.finish(ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        reader.read(1)


def test_cierre_desbloquea_la_lectura():
    reader = StreamReader(1024)
    result: dict[str, bytes] = {}

    def worker() -> None:
        result["data"] = reader.read(4)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    reader.close()
    thread.join(2)
    assert result["data"] == b""


def test_contrapresion_limita_la_memoria():
    reader = StreamReader(8)
    done = threading.Event()

    def producer() -> None:
        reader.feed(b"x" * 8)
        reader.feed(b"y" * 8)  # debe bloquearse hasta que se libere espacio
        done.set()

    thread = threading.Thread(target=producer)
    thread.start()
    time.sleep(0.05)
    assert not done.is_set()
    assert reader.read(8) == b"x" * 8
    thread.join(2)
    assert done.is_set()
    assert reader.read(8) == b"y" * 8


def test_rechunk_no_aplica_pero_lee_exacto_entre_bloques():
    reader = StreamReader(1024)
    reader.feed(b"0123456789")
    reader.finish()
    assert reader.read(3) == b"012"
    assert reader.read(3) == b"345"
    assert reader.read(3) == b"678"
    assert reader.read(3) == b"9"
    assert reader.read(3) == b""
