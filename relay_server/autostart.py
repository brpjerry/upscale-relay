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

import os
import shlex
import shutil
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Upscale Relay Server"
_DESKTOP_NAME = "upscale-relay-server.desktop"


def is_supported() -> bool:
    return sys.platform == "win32" or sys.platform.startswith("linux")


def sign_in_label() -> str:
    if sys.platform == "win32":
        return "Start automatically when I sign in to Windows"
    return "Start automatically when I sign in"


def launch_command() -> str:
    """The command to run at sign-in, quoted for the Run key or Exec=."""
    if getattr(sys, "frozen", False):
        # PyInstaller build: sys.executable is upscale-relay-server-gui.exe.
        return f'"{sys.executable}"'
    script = shutil.which("relay-server-gui")
    if script:
        return f'"{script}"'
    return f'"{sys.executable}" -m relay_server.tray'


def _desktop_entry() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config) if config else Path.home() / ".config"
    return root / "autostart" / _DESKTOP_NAME


def _desktop_contents() -> str:
    script = shutil.which("relay-server-gui")
    command = (
        shlex.quote(script) if script
        else f"{shlex.quote(sys.executable)} -m relay_server.tray"
    )
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_VALUE_NAME}\n"
        "Comment=Upscale Relay server in the system tray\n"
        f"Exec={command}\n"
        "Icon=video-display\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def is_enabled() -> bool:
    if not is_supported():
        return False
    if sys.platform != "win32":
        return _desktop_entry().is_file()
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
