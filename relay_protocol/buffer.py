"""The client buffer the server keeps topped off (docs/PROTOCOL.md §5).

A client asks for its target in ``open_session.buffer_target_ms``; the server
applies it clamped to this range and confirms the value in
``session_opened.buffer_target_ms``. A server that predates the field (or a
client that sends none) keeps the default.
"""

from __future__ import annotations

import math

DEFAULT_BUFFER_TARGET_MS = 10_000
MIN_BUFFER_TARGET_MS = 2_000
MAX_BUFFER_TARGET_MS = 300_000


def clamp_buffer_target_ms(value: int | float) -> int:
    """A requested target as the server applies it."""
    return max(MIN_BUFFER_TARGET_MS, min(MAX_BUFFER_TARGET_MS, int(round(value))))


def parse_buffer_target_ms(value: object) -> int | None:
    """A wire value as a clamped target; None when it is not a finite number.

    bool is an int in Python but never a duration on the wire.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return clamp_buffer_target_ms(value)
