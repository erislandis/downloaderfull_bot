from __future__ import annotations

import httpx

from .base import RemoteFile, Source
from .mediafire import MediaFireSource
from .mega import MegaSource

SOURCES: tuple[type[Source], ...] = (MediaFireSource, MegaSource)


def resolve_source(url: str, http: httpx.AsyncClient) -> Source | None:
    for cls in SOURCES:
        if cls.matches(url):
            return cls(http)
    return None


__all__ = ["RemoteFile", "Source", "SOURCES", "resolve_source"]
