"""Start-with-Windows registration for the tray GUI (HKCU Run key).

Only the tray GUI offers this; the headless ``relay-server`` CLI has no
autostart concept. The registry value itself is the persisted state — nothing
is mirrored into QSettings, so the checkbox always reflects what Windows will
actually do at sign-in (including a value the user deleted by hand).
"""

from __future__ import annotations

import shutil
import sys

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Upscale Relay Server"


def is_supported() -> bool:
    return sys.platform == "win32"


def launch_command() -> str:
    """The command Windows should run at sign-in, quoted for the Run key."""
    if getattr(sys, "frozen", False):
        # PyInstaller build: sys.executable is upscale-relay-server-gui.exe.
        return f'"{sys.executable}"'
    script = shutil.which("relay-server-gui")
    if script:
        return f'"{script}"'
    return f'"{sys.executable}" -m relay_server.tray'


def is_enabled() -> bool:
    if not is_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """Create or remove the Run-key value. Raises OSError on registry failure."""
    if not is_supported():
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
