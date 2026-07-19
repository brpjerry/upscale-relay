"""Persisted relay-server settings for the Windows tray GUI (QSettings-backed).

Kept separate from the tray widgets so the persistence layer is importable and
testable without constructing any Qt windows. Mirrors
``desktop_client/settings.py`` — same org, a distinct application name so the
server and desktop client never share a settings tree.
"""

from __future__ import annotations

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


# Backward-compatible snapshot for callers that only need import-time choices.
# The GUI itself calls available_ep_choices() after first-run setup completes.
EP_CHOICES: tuple[str, ...] = available_ep_choices()


class ServerSettings:
    def __init__(self, scope: str | None = None):
        # Tests pass an isolated scope so they never touch the user's real
        # server configuration.
        self._qs = QSettings(_ORG, scope or _APP)

    @property
    def models_dir(self) -> str:
        return str(self._qs.value("server/models_dir", "models"))

    @models_dir.setter
    def models_dir(self, v: str) -> None:
        self._qs.setValue("server/models_dir", str(v))

    @property
    def library_dir(self) -> str:
        # Empty string => no media library exposed (server --library omitted).
        return str(self._qs.value("server/library_dir", ""))

    @library_dir.setter
    def library_dir(self, v: str) -> None:
        self._qs.setValue("server/library_dir", str(v))

    @property
    def port(self) -> int:
        return int(self._qs.value("server/port", 8590))

    @port.setter
    def port(self, v: int) -> None:
        self._qs.setValue("server/port", int(v))

    @property
    def ep(self) -> str:
        value = str(self._qs.value("server/ep", "auto"))
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
