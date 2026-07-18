"""Best-effort theme adoption when running on the bundled (pip-wheel) Qt.

The PySide6 wheel ships a private Qt with only the Fusion/Windows styles and
no platform-theme plugins, so QT_QPA_PLATFORMTHEME (qt6ct, kde) is silently
ignored: no system widget style, icon theme, or colors. When that happens,
read the user's qt6ct config and apply what we can — the icon theme by name,
and the KDE color scheme mapped onto Fusion. A distro PySide6 (system Qt)
loads the real platform theme, in which case this module backs off entirely.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

# QPalette role -> (KDE color-scheme section, key)
_ROLES = {
    QPalette.Window: ("Colors:Window", "BackgroundNormal"),
    QPalette.WindowText: ("Colors:Window", "ForegroundNormal"),
    QPalette.Base: ("Colors:View", "BackgroundNormal"),
    QPalette.AlternateBase: ("Colors:View", "BackgroundAlternate"),
    QPalette.Text: ("Colors:View", "ForegroundNormal"),
    QPalette.PlaceholderText: ("Colors:View", "ForegroundInactive"),
    QPalette.Link: ("Colors:View", "ForegroundLink"),
    QPalette.Button: ("Colors:Button", "BackgroundNormal"),
    QPalette.ButtonText: ("Colors:Button", "ForegroundNormal"),
    QPalette.Highlight: ("Colors:Selection", "BackgroundNormal"),
    QPalette.HighlightedText: ("Colors:Selection", "ForegroundNormal"),
    QPalette.ToolTipBase: ("Colors:Tooltip", "BackgroundNormal"),
    QPalette.ToolTipText: ("Colors:Tooltip", "ForegroundNormal"),
}


def _read_ini(path: Path) -> configparser.ConfigParser | None:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # KDE keys are CamelCase
    try:
        if not cp.read(path):
            return None
    except (configparser.Error, OSError):
        return None
    return cp


def _palette_from_kde_scheme(scheme: configparser.ConfigParser) -> QPalette:
    palette = QPalette()
    for role, (section, key) in _ROLES.items():
        try:
            r, g, b = (int(x) for x in scheme[section][key].split(",")[:3])
        except (KeyError, ValueError):
            continue
        palette.setColor(role, QColor(r, g, b))
    # KDE schemes carry no disabled group; blend foreground into background.
    fg = palette.color(QPalette.WindowText)
    bg = palette.color(QPalette.Window)
    dis = QColor((fg.red() + bg.red()) // 2, (fg.green() + bg.green()) // 2,
                 (fg.blue() + bg.blue()) // 2)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, dis)
    return palette


def apply_system_theme(app: QApplication) -> None:
    if QIcon.themeName() not in ("", "hicolor"):
        return  # a real platform theme loaded — nothing to fix
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    conf = _read_ini(config_home / "qt6ct" / "qt6ct.conf")
    if conf is None or "Appearance" not in conf:
        return
    icon_theme = conf["Appearance"].get("icon_theme", "")
    if icon_theme:
        QIcon.setThemeName(icon_theme)
    scheme_path = conf["Appearance"].get("color_scheme_path", "")
    if scheme_path:
        scheme = _read_ini(Path(scheme_path))
        if scheme is not None:
            app.setStyle("Fusion")  # setStyle resets the palette; ours goes after
            app.setPalette(_palette_from_kde_scheme(scheme))
