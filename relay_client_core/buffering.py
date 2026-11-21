"""Sizing the server's buffer target to a client's cache (docs/PROTOCOL.md §5).

Kept free of Qt and mpv: the desktop player calls it with what mpv holds.
"""

from __future__ import annotations

# A sample shorter than this is trusted only when it covers the whole cache:
# extrapolating from the first fraction of a second (often a keyframe or a
# black opening) misjudges the bitrate by orders of magnitude.
_MIN_SAMPLE_MS = 1000
_FULL_CACHE = 0.9


def cache_target_ms(cache_bytes: int, cached_ms: int, cached_bytes: object) -> int | None:
    """How long a buffer fills ``cache_bytes`` at the bitrate of what the
    player holds (``cached_bytes`` over ``cached_ms``).

    None while the sample is too short to say: under a second, unless the
    cache is already full, in which case it holds all the video it can and
    the target is what it holds (a stream above ~2 Gbps fills 256 MiB in
    under a second).
    """
    if cached_ms <= 0 or type(cached_bytes) is not int or cached_bytes <= 0:
        return None
    if cached_ms < _MIN_SAMPLE_MS and cached_bytes < _FULL_CACHE * cache_bytes:
        return None
    return round(cache_bytes * cached_ms / cached_bytes)
