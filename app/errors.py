"""Excepciones del proyecto. El mensaje de una BridgeError es apto para el usuario final."""
from __future__ import annotations


class BridgeError(Exception):
    """Error esperado: su mensaje se muestra tal cual al usuario."""


class SourceError(BridgeError):
    """Problema con el enlace o el sitio de origen (reintentar no lo arregla)."""


class QuotaExceeded(SourceError):
    """El sitio de origen limitó la transferencia (p. ej. cuota de Mega)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(BridgeError):
    """Fallo temporal (red, 5xx, límite de peticiones): se puede reintentar."""


class ResumeNotSupported(BridgeError):
    """El origen o la sesión de subida no permite reanudar: hay que empezar de cero."""


class DriveError(BridgeError):
    """Error de Google Drive."""


class Realign(Exception):
    """Drive confirmó menos bytes de los enviados; se reanuda desde `offset`."""

    def __init__(self, offset: int) -> None:
        super().__init__(offset)
        self.offset = offset
