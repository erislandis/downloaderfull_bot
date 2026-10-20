from __future__ import annotations

import re

_UNITS = ("B", "KB", "MB", "GB", "TB")
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def human_size(n: int | None) -> str:
    if n is None:
        return "tamaño desconocido"
    value, i = float(n), 0
    while value >= 1024 and i < len(_UNITS) - 1:
        value /= 1024
        i += 1
    return f"{int(value)} B" if i == 0 else f"{value:.1f} {_UNITS[i]}"


def human_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def safe_filename(name: str, default: str = "archivo") -> str:
    cleaned = _FORBIDDEN.sub("_", name).strip(" .")
    return cleaned[:200] or default


def progress_bar(fraction: float, width: int = 12) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return "▰" * filled + "▱" * (width - filled)
