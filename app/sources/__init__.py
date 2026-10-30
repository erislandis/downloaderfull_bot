from __future__ import annotations

import httpx

from .base import RemoteFile, Source
from .mediafire import MediaFireSource
from .mega import MegaSource

SOURCES: tuple[type[Source], ...] = (MediaFireSource, MegaSource)


def resolve_source(
    url: str,
    http: httpx.AsyncClient,
    *,
    mediafire_http: httpx.AsyncClient | None = None,
) -> Source | None:
    for cls in SOURCES:
        if cls.matches(url):
            if cls is MediaFireSource and mediafire_http is not None:
                return cls(mediafire_http)
            return cls(http)
    return None


__all__ = ["RemoteFile", "Source", "SOURCES", "resolve_source"]
