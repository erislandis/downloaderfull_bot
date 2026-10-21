from __future__ import annotations

import httpx

from .base import RemoteFile, Source, SourceOptions
from .direct import DirectSource
from .gdrive import GoogleDriveSource
from .mediafire import MediaFireSource
from .mega import MegaSource

# El orden importa: se usa el primer origen que reconozca el enlace. ``DirectSource``
# acepta cualquier http(s), así que tiene que ir el último.
SOURCES: tuple[type[Source], ...] = (MediaFireSource, MegaSource, GoogleDriveSource, DirectSource)


def resolve_source(
    url: str, http: httpx.AsyncClient, zip_level: int = 1, options: SourceOptions | None = None
) -> Source | None:
    for cls in SOURCES:
        if cls.matches(url):
            return cls(http, zip_level, options)
    return None


__all__ = ["RemoteFile", "Source", "SourceOptions", "SOURCES", "resolve_source"]
