"""Persisted app settings (QSettings-backed)."""

from __future__ import annotations

from PySide6.QtCore import QSettings

_ORG = "upscale-relay"
_APP = "desktop-client"

FAST_FORWARD_DEFAULT_S = 85  # 1:25, a typical opening sequence
FAST_FORWARD_MAX_S = 3600


def _clamp_fast_forward(value: int) -> int:
    return max(1, min(FAST_FORWARD_MAX_S, int(value)))


def format_skip(seconds: int) -> str:
    """Compact m:ss label for the fast-forward button ("1:25", "0:30")."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


class AppSettings:
    def __init__(self, scope: str | None = None):
        # Tests pass an isolated scope so they never touch the user's real
        # settings (a smoke test once left the user on lossless-ffv1).
        app = scope or _APP
        self._qs = QSettings(_ORG, app)

    @property
    def server_host(self) -> str:
        return self._qs.value("server/host", "127.0.0.1")

    @server_host.setter
    def server_host(self, v: str) -> None:
        self._qs.setValue("server/host", v)

    @property
    def server_port(self) -> int:
        return int(self._qs.value("server/port", 8590))

    @server_port.setter
    def server_port(self, v: int) -> None:
        self._qs.setValue("server/port", int(v))

    @property
    def auto_connect(self) -> bool:
        return self._qs.value("server/auto_connect", False, type=bool)

    @auto_connect.setter
    def auto_connect(self, v: bool) -> None:
        self._qs.setValue("server/auto_connect", bool(v))

    @property
    def browser_visible(self) -> bool:
        return self._qs.value("browser/visible", True, type=bool)

    @browser_visible.setter
    def browser_visible(self, v: bool) -> None:
        self._qs.setValue("browser/visible", bool(v))

    @property
    def model(self) -> str:
        return self._qs.value("session/model", "passthrough")

    @model.setter
    def model(self, v: str) -> None:
        self._qs.setValue("session/model", v)

    @property
    def quality_tier(self) -> str:
        value = self._qs.value("session/tier", "lossless-hevc")
        # The former single lossy tier was closest to the new low-bandwidth
        # option. Migrate it without leaving a dead combo-box selection.
        return "hevc-qp18" if value == "visually-lossless" else value

    @quality_tier.setter
    def quality_tier(self, v: str) -> None:
        self._qs.setValue("session/tier", v)

    @property
    def fit_mode(self) -> str:
        return self._qs.value("session/fit_mode", "fit")

    @fit_mode.setter
    def fit_mode(self, v: str) -> None:
        self._qs.setValue("session/fit_mode", v)

    @property
    def resize_algorithm(self) -> str:
        return self._qs.value("session/resize_algorithm", "")

    @resize_algorithm.setter
    def resize_algorithm(self, v: str) -> None:
        self._qs.setValue("session/resize_algorithm", v)

    @property
    def deband_enabled(self) -> bool:
        return self._qs.value("playback/deband", False, type=bool)

    @deband_enabled.setter
    def deband_enabled(self, v: bool) -> None:
        self._qs.setValue("playback/deband", bool(v))

    @property
    def display_sync(self) -> str:
        """When to tell mpv the display's refresh rate (so its display-sync
        modes work): "off", "windowed" (not while settled in fullscreen), or
        "always"."""
        value = self._qs.value("playback/display_sync", "off")
        if value in (True, "true"):  # the earlier on/off switch
            return "windowed"
        return value if value in ("off", "windowed", "always") else "off"

    @display_sync.setter
    def display_sync(self, v: str) -> None:
        self._qs.setValue("playback/display_sync", v if v in ("off", "windowed", "always") else "off")

    @property
    def fast_forward_s(self) -> int:
        return _clamp_fast_forward(self._qs.value("playback/fast_forward_s", FAST_FORWARD_DEFAULT_S, type=int))

    @fast_forward_s.setter
    def fast_forward_s(self, v: int) -> None:
        self._qs.setValue("playback/fast_forward_s", _clamp_fast_forward(v))

    @property
    def theme_mode(self) -> str:
        value = self._qs.value("appearance/theme", "auto")
        return value if value in ("auto", "dark", "light") else "auto"

    @theme_mode.setter
    def theme_mode(self, v: str) -> None:
        self._qs.setValue("appearance/theme", v)

    @property
    def accent(self) -> str:
        """"auto" (follow the video) or a "#rrggbb" colour."""
        value = str(self._qs.value("appearance/accent", "auto"))
        return value if value == "auto" or (len(value) == 7 and value.startswith("#")) else "auto"

    @accent.setter
    def accent(self, v: str) -> None:
        self._qs.setValue("appearance/accent", v)

    @property
    def browse_dir(self) -> str:
        return self._qs.value("browser/dir", "")

    @browse_dir.setter
    def browse_dir(self, v: str) -> None:
        self._qs.setValue("browser/dir", v)

    @property
    def autoplay(self) -> bool:
        return self._qs.value("library/autoplay", True, type=bool)

    @autoplay.setter
    def autoplay(self, value: bool):
        self._qs.setValue("library/autoplay", value)

    @property
    def history_limit(self) -> int:
        return max(1, min(1000, self._qs.value("library/history_limit", 50, type=int)))

    @history_limit.setter
    def history_limit(self, value: int):
        self._qs.setValue("library/history_limit", max(1, min(1000, value)))

    @property
    def browser_sort(self) -> str:
        value = self._qs.value("browser/sort", "name")
        return value if value in ("name", "mtime") else "name"

    @browser_sort.setter
    def browser_sort(self, value: str):
        self._qs.setValue("browser/sort", value)

    @property
    def diagnostics(self) -> bool:
        return self._qs.value("diagnostics/display", False, type=bool)

    @diagnostics.setter
    def diagnostics(self, value: bool):
        self._qs.setValue("diagnostics/display", value)

    @property
    def file_logging(self) -> bool:
        return self._qs.value("diagnostics/file_logging", False, type=bool)

    @file_logging.setter
    def file_logging(self, value: bool):
        self._qs.setValue("diagnostics/file_logging", value)
