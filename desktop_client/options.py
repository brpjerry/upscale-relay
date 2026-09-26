"""Command-line-configurable desktop client behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class DesktopOptions:
    debug: bool = False
    trace: bool = False
    mpv_osc: bool = False
    no_hwdec: bool = False
    mpv_scripts: bool = False
    headless: bool = False
    settings_scope: str | None = None
    mpv_config_path: Path | None = None
    log_root: Path | None = None
    discovery: bool | None = None

    def __post_init__(self):
        # An isolated scope also isolates native config/logs. Existing GUI tests
        # cannot accidentally read or edit the user's standalone mpv defaults.
        if self.settings_scope or self.headless:
            root = Path(tempfile.mkdtemp(prefix="relay-desktop-"))
            if self.mpv_config_path is None:
                object.__setattr__(self, "mpv_config_path", root / "mpv.conf")
            if self.log_root is None:
                object.__setattr__(self, "log_root", root / "logs")
        if self.discovery is None:
            object.__setattr__(self, "discovery", not (self.headless or self.settings_scope))
