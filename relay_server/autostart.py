"""Start-at-sign-in registration for the tray GUI.

Windows: the HKCU Run key.  Linux: an XDG autostart entry
(``$XDG_CONFIG_HOME/autostart``), which the session starts at login; the
headless server's equivalent there is its systemd user unit.

Only the tray GUI offers this; the headless ``relay-server`` CLI has no
autostart concept. The registration itself is the persisted state — nothing
is mirrored into QSettings, so the checkbox always reflects what the system
will actually do at sign-in (including an entry the user deleted by hand).
"""

from __future__ import annotations

import configparser
import os
import shutil
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Upscale Relay Server"
_DESKTOP_NAME = "upscale-relay-server.desktop"
# Marks a sign-in launch, which waits for the tray host instead of failing
# when the session has not started it yet.
AUTOSTART_ARG = "--autostart"


def is_supported() -> bool:
    return sys.platform == "win32" or sys.platform.startswith("linux")


def sign_in_label() -> str:
    if sys.platform == "win32":
        return "Start automatically when I sign in to Windows"
    return "Start automatically when I sign in"


def launch_command() -> str:
    """The command Windows runs at sign-in, quoted for the Run key."""
    if getattr(sys, "frozen", False):
        # PyInstaller build: sys.executable is upscale-relay-server-gui.exe.
        return f'"{sys.executable}" {AUTOSTART_ARG}'
    script = shutil.which("relay-server-gui")
    if script:
        return f'"{script}" {AUTOSTART_ARG}'
    return f'"{sys.executable}" -m relay_server.tray {AUTOSTART_ARG}'


def _desktop_entry() -> Path:
    # XDG base directories must be absolute; a relative value is ignored.
    config = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    if not config.is_absolute():
        config = Path.home() / ".config"
    return config / "autostart" / _DESKTOP_NAME


def _exec_argument(argument: str) -> str:
    """Quote one argument for a desktop entry's Exec key.

    The Desktop Entry spec quotes with double quotes and backslash-escapes
    ``"`$\\`` inside them; the key's string value then escapes backslashes
    once more, and ``%`` (field codes) is written ``%%``.
    """
    quoted = "".join("\\" + c if c in '"`$\\' else c for c in argument)
    quoted = quoted.replace("\\", "\\\\").replace("%", "%%")
    return f'"{quoted}"'


def _desktop_contents() -> str:
    script = shutil.which("relay-server-gui")
    command = (
        [script] if script else [sys.executable, "-m", "relay_server.tray"]
    )
    exec_line = " ".join(_exec_argument(part) for part in command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_VALUE_NAME}\n"
        "Comment=Upscale Relay server in the system tray\n"
        f"Exec={exec_line} {AUTOSTART_ARG}\n"
        "Icon=video-display\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _desktop_entry_enabled() -> bool:
    """An entry a startup-apps manager disabled (Hidden=true, or GNOME's
    X-GNOME-Autostart-enabled=false) stays on disk but does not start."""
    entry_path = _desktop_entry()
    if not entry_path.is_file():
        return False
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # desktop entry keys are case-sensitive
    try:
        parser.read(entry_path, encoding="utf-8")
        entry = parser["Desktop Entry"]
        if entry.getboolean("Hidden", fallback=False):
            return False
        return entry.getboolean("X-GNOME-Autostart-enabled", fallback=True)
    except (KeyError, ValueError, configparser.Error):
        return False


def is_enabled() -> bool:
    if not is_supported():
        return False
    if sys.platform != "win32":
        return _desktop_entry_enabled()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """Create or remove the registration. Raises OSError on failure."""
    if not is_supported():
        return
    if sys.platform != "win32":
        # Applying unrelated settings must not rewrite (and so re-enable) an
        # entry the user's desktop manages.
        if enabled == is_enabled():
            return
        entry = _desktop_entry()
        if enabled:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(_desktop_contents(), encoding="utf-8")
        else:
            entry.unlink(missing_ok=True)
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
