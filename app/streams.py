"""Puente sync/async para subir un flujo de bytes a un consumidor síncrono.

Telethon lee el archivo con ``read(n)`` de forma sincrónica desde su propio hilo,
mientras el ``stream()`` asíncrono del origen corre en el loop principal. Esta cola
acotada (thread-safe) conecta ambos mundos con memoria constante.
"""
from __future__ import annotations

import collections
import io
import threading


class StreamReader(io.RawIOBase):
    """Archivo de lectura que recibe datos desde otro hilo y los sirve a ``read()``.

    La memoria usada está limitada por ``max_buffered`` bytes en cola.
    """

    def __init__(self, max_buffered: int, name: str = "archivo.bin") -> None:
        super().__init__()
        self.name = name
        self._cond = threading.Condition()
        self._chunks: collections.deque[bytes] = collections.deque()
        self._buffered = 0
        self._max = max_buffered
        self._eof = False
        self._error: BaseException | None = None
        self._rem = b""
        self._closed = False

    @property
    def error(self) -> BaseException | None:
        return self._error

    def feed(self, chunk: bytes) -> None:
        """Encola un bloque (lo llama el hilo productor; espera si está lleno)."""
        with self._cond:
            while self._buffered >= self._max and not self._closed:
                self._cond.wait()
            if self._closed or not chunk:
                return
            self._chunks.append(chunk)
            self._buffered += len(chunk)
            self._cond.notify_all()

    def finish(self, error: BaseException | None = None) -> None:
        """Marca el final del flujo (o propaga el error que lo cortó)."""
        with self._cond:
            self._eof = True
            self._error = error
            self._cond.notify_all()

    def readable(self) -> bool:
        return True

    def readall(self) -> bytes:
        parts: list[bytes] = []
        while True:
            data = self.read(64 * 1024)
            if not data:
                break
            parts.append(data)
        return b"".join(parts)

    def readinto(self, buffer) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            raise ValueError("StreamReader no admite read() sin tamaño")
        out = bytearray()
        while len(out) < size and not self._closed:
            if self._rem:
                take = min(size - len(out), len(self._rem))
                out += self._rem[:take]
                self._rem = self._rem[take:]
                continue
            with self._cond:
                while not self._chunks and not self._eof and not self._closed:
                    self._cond.wait()
                if self._closed:
                    break
                if not self._chunks:
                    if self._error is not None:
                        raise self._error
                    break  # fin normal del flujo
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._cond.notify_all()
            take = min(size - len(out), len(chunk))
            out += chunk[:take]
            self._rem = chunk[take:]
        return bytes(out)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        super().close()
