"""Persisted relay-server settings for the tray GUI (QSettings-backed).

Kept separate from the tray widgets so the persistence layer is importable and
testable without constructing any Qt windows. Mirrors
``desktop_client/settings.py`` — same org, a distinct application name so the
server and desktop client never share a settings tree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings

_ORG = "upscale-relay"
_APP = "server"

_PROVIDER_ALIASES = (
    ("tensorrt", "TensorrtExecutionProvider"),
    ("cuda", "CUDAExecutionProvider"),
    ("dml", "DmlExecutionProvider"),
    ("cpu", "CPUExecutionProvider"),
)


def available_ep_choices() -> tuple[str, ...]:
    """Only offer providers registered in the installed ORT build."""
    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except ImportError:
        return ("auto",)
    return ("auto", *(alias for alias, name in _PROVIDER_ALIASES if name in available))


def __getattr__(name: str):
    # EP_CHOICES used to be computed at import, which imported onnxruntime
    # before the tray could activate the managed runtime: on Linux a system
    # python-onnxruntime then won for the whole process.
    if name == "EP_CHOICES":
        return available_ep_choices()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_models_dir() -> str:
    """Linux: the folder the packaged systemd unit serves, so switching
    between the unit and the tray keeps the same models.  Elsewhere the
    historical ./models."""
    if not sys.platform.startswith("linux"):
        return "models"
    data_home = Path(os.environ.get("XDG_DATA_HOME") or "")
    if not data_home.is_absolute():
        data_home = Path.home() / ".local" / "share"
    return str(data_home / "upscale-relay" / "models")


class ServerSettings:
    def __init__(self, scope: str | None = None):
        # Tests pass an isolated scope so they never touch the user's real
        # server configuration.
        self._qs = QSettings(_ORG, scope or _APP)

    @property
    def models_dir(self) -> str:
        return str(self._qs.value("server/models_dir", default_models_dir()))

    @models_dir.setter
    def models_dir(self, v: str) -> None:
        self._qs.setValue("server/models_dir", str(v))

    @property
    def library_dirs(self) -> list[str]:
        """Configured media roots, migrating the former single-root setting."""
        value = self._qs.value("server/library_dirs", None)
        if value is None:
            legacy = str(self._qs.value("server/library_dir", "")).strip()
            return [legacy] if legacy else []
        values = [value] if isinstance(value, str) else list(value)
        return [str(item).strip() for item in values if str(item).strip()]

    @library_dirs.setter
    def library_dirs(self, values: list[str]) -> None:
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        self._qs.setValue("server/library_dirs", cleaned)
        self._qs.remove("server/library_dir")

    @property
    def library_dir(self) -> str:
        """Compatibility view of the first configured media root."""
        return self.library_dirs[0] if self.library_dirs else ""

    @library_dir.setter
    def library_dir(self, v: str) -> None:
        value = str(v).strip()
        self.library_dirs = [value] if value else []

    @property
    def port(self) -> int:
        return int(self._qs.value("server/port", 8590))

    @port.setter
    def port(self, v: int) -> None:
        self._qs.setValue("server/port", int(v))

    @property
    def requested_ep(self) -> str:
        """The saved provider, without asking onnxruntime whether it exists:
        safe before the runtime is activated."""
        value = str(self._qs.value("server/ep", "auto"))
        return value if any(value == alias for alias, _ in _PROVIDER_ALIASES) else "auto"

    @property
    def ep(self) -> str:
        value = self.requested_ep
        return value if value in available_ep_choices() else "auto"

    @ep.setter
    def ep(self, v: str) -> None:
        self._qs.setValue("server/ep", str(v))

    @property
    def mdns(self) -> bool:
        return self._qs.value("server/mdns", True, type=bool)

    @mdns.setter
    def mdns(self, v: bool) -> None:
        self._qs.setValue("server/mdns", bool(v))

    @property
    def file_logging(self) -> bool:
        return self._qs.value("server/file_logging", True, type=bool)

    @file_logging.setter
    def file_logging(self, v: bool) -> None:
        self._qs.setValue("server/file_logging", bool(v))
