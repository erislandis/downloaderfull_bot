"""Contrato común de los sitios de origen (MediaFire, Mega, ...)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, ClassVar

import httpx


@dataclass(frozen=True)
class RemoteFile:
    """Archivo descargable. `ref` es opaco: solo lo interpreta su Source."""

    name: str
    size: int | None
    ref: str


class Source(ABC):
    key: ClassVar[str]
    label: ClassVar[str]

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        """¿Este enlace pertenece a este sitio?"""

    @abstractmethod
    async def inspect(self, url: str) -> list[RemoteFile]:
        """Lista los archivos del enlace (1 para un archivo, N para una carpeta)."""

    @abstractmethod
    def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        """Entrega el contenido (ya descifrado) desde `offset`.

        Si el origen no puede reanudar, debe lanzar ResumeNotSupported.
        """
