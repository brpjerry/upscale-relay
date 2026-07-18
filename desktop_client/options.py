"""Command-line-configurable desktop client behavior."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopOptions:
    debug: bool = False
    trace: bool = False
    mpv_osc: bool = False
    no_hwdec: bool = False
    mpv_scripts: bool = False
    headless: bool = False
    settings_scope: str | None = None
