"""Chapter-list helpers: pure functions shared by the UI and tests.

Chapters arrive as wire-format dicts ({start_s, end_s?, title?}, see
docs/PROTOCOL.md session_opened) and are normalized once into Chapter tuples;
everything downstream (combo labels, slider marks, prev/next targets) works on
that normalized list.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# "Previous chapter" returns to the start of the current chapter once playback
# is this far into it, matching every mainstream player's back button.
PREVIOUS_RESTART_THRESHOLD_S = 3.0


@dataclass(frozen=True)
class Chapter:
    start_s: float
    title: str


def normalize_chapters(raw: list | None) -> list[Chapter]:
    """Wire dicts -> sorted Chapter list with non-empty display titles."""
    if not raw:
        return []
    chapters = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start_s = item.get("start_s")
        if not isinstance(start_s, (int, float)) or start_s < 0:
            continue
        chapters.append((float(start_s), item.get("title")))
    chapters.sort(key=lambda pair: pair[0])
    return [
        Chapter(start_s, title if title else f"Chapter {index + 1}")
        for index, (start_s, title) in enumerate(chapters)
    ]


def chapter_index(chapters: list[Chapter], pos_s: float) -> int | None:
    """Index of the chapter containing pos_s (last start <= pos), or None."""
    if not chapters:
        return None
    index = bisect_right([c.start_s for c in chapters], pos_s) - 1
    return index if index >= 0 else None


def step_target(chapters: list[Chapter], pos_s: float, delta: int) -> float | None:
    """Seek target for a prev(-1)/next(+1) chapter step, or None for no-op.

    Next past the last chapter and prev before the first both return None.
    Prev inside a chapter's opening seconds goes to the chapter before it.
    """
    if not chapters or delta == 0:
        return None
    current = chapter_index(chapters, pos_s)
    if delta > 0:
        index = (current if current is not None else -1) + 1
        return chapters[index].start_s if index < len(chapters) else None
    if current is None:
        return None
    if pos_s - chapters[current].start_s > PREVIOUS_RESTART_THRESHOLD_S:
        return chapters[current].start_s
    return chapters[current - 1].start_s if current > 0 else 0.0


def slider_fractions(chapters: list[Chapter], duration_s: float | None) -> list[float]:
    """Chapter starts as 0..1 fractions of the duration (for slider ticks)."""
    if not chapters or not duration_s or duration_s <= 0:
        return []
    return [c.start_s / duration_s for c in chapters if 0 < c.start_s < duration_s]
