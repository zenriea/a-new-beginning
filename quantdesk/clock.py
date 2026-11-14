"""Single source of time so tests can simulate outages, sessions and restarts."""
from __future__ import annotations

import time as _time

_offset: float | None = None  # when set, now() returns this fixed epoch


def now() -> float:
    return _offset if _offset is not None else _time.time()


def set_fixed(epoch: float | None) -> None:
    global _offset
    _offset = epoch


def advance(seconds: float) -> None:
    global _offset
    if _offset is None:
        _offset = _time.time()
    _offset += seconds
