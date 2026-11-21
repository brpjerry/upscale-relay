"""The client buffer the server keeps topped off (docs/PROTOCOL.md §5).

A client asks for its target in ``open_session.buffer_target_ms`` and may
update it in any ``buffer_report``; the server confirms the opening value in
``session_opened.buffer_target_ms``. A server that predates the field (or a
client that sends none) keeps the default. There is no maximum: clients size
the target for the memory they hold it in.
"""

from __future__ import annotations

import math

DEFAULT_BUFFER_TARGET_MS = 10_000
# The server resumes 500 ms below the target and reports arrive every 500 ms:
# a target near that would pause and resume on every report.
MIN_BUFFER_TARGET_MS = 2_000


def parse_buffer_target_ms(value: object) -> int | None:
    """A wire value as the target the server applies; None when it is not a
    finite number. bool is an int in Python but never a duration on the wire.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # (an int is always finite; isfinite would overflow on a huge one)
    return max(MIN_BUFFER_TARGET_MS, int(round(value)))
