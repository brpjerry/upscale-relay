"""Flat dark/light theme shared by every desktop-client widget.

The palette, radii, motion values and icon set mirror the MV Player look:
near-black layered surfaces, one accent colour, round icon buttons and
Material icon outlines. Text and window colours travel through QPalette (so
a widget that overrides its palette — the fullscreen control overlay — stays
readable); shapes come from the application style sheet built here, and the
custom-painted widgets in ``widgets.py`` read ``current()`` at paint time.
"""

from __future__ import annotations

import atexit
import math
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

RADIUS = 10
RADIUS_SMALL = 6
# Motion (ms)
FAST = 110
NORMAL = 190
SLOW = 340

DEFAULT_ACCENT = "#8b7dff"
ACCENT_PRESETS = (
    "#8b7dff", "#5b9dff", "#3ec9d6", "#4fd68f", "#c5d94a", "#f5b84a", "#ff8f4d", "#ff6f91", "#f06bd8",
)
ACCENT_FADE = 300        # a colour the user picked
ACCENT_FADE_AUTO = 800   # a colour following the video
_ACCENT_LUMINANCE = 0.33  # relative luminance every accent shares
# Things drawn over video look the same in both modes.
SCRIM = "#0c0d10"
SCRIM_TEXT = "#eef0f4"
SCRIM_TEXT_DIM = "#9ba2af"

_DARK = dict(
    bg="#0c0d10", surface="#121419", raised="#191c22", hover="#1f232b",
    pressed="#272c36", line="#20242c", text="#eef0f4", text_dim="#9ba2af",
    text_faint="#636a78", good="#4fd6a2", warn="#f2b866", bad="#ff7676",
)
_LIGHT = dict(
    bg="#f5f6f8", surface="#eceef2", raised="#ffffff", hover="#e2e5eb",
    pressed="#d3d7df", line="#d9dce3", text="#15171b", text_dim="#555c69",
    text_faint="#878e9b", good="#1c9a6b", warn="#b27610", bad="#d24444",
)


class Icons:
    """24×24 icon outlines (Material Design icons, Apache-2.0) as SVG path data."""

    play = "M8 5v14l11-7z"
    pause = "M6 19h4V5H6v14zm8-14v14h4V5h-4z"
    stop = "M6 6h12v12H6z"
    next = "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"
    fast_forward = "M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z"
    fast_rewind = "M11 18V6l-8.5 6 8.5 6zm.5-6l8.5 6V6l-8.5 6z"
    previous = "M6 6h2v12H6zm3.5 6l8.5 6V6z"
    fullscreen = "M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"
    fullscreen_exit = "M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"
    close = "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"
    chevron_down = "M16.59 8.59L12 13.17 7.41 8.59 6 10l6 6 6-6z"
    chevron_up = "M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z"
    chevron_right = "M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"
    volume_high = "M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"
    volume_low = "M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z"
    volume_off = "M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"
    settings = "M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"
    folder = "M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"
    refresh = "M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"
    movie = "M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"
    arrow_up = "M4 12l1.41 1.41L11 7.83V20h2V7.83l5.58 5.59L20 12l-8-8-8 8z"
    home = "M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"
    menu = "M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z"
    subtitles = "M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zM4 12h4v2H4v-2zm10 6H4v-2h10v2zm6 0h-4v-2h4v2zm0-4H10v-2h10v2z"
    nearby = "M1 9l2 2c4.97-4.97 13.03-4.97 18 0l2-2C16.93 2.93 7.08 2.93 1 9zm8 8l3 3 3-3c-1.65-1.66-4.34-1.66-6 0zm-4-4l2 2c2.76-2.76 7.24-2.76 10 0l2-2C15.14 9.14 8.87 9.14 5 13z"
    dns = "M20 13H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1v-6c0-.55-.45-1-1-1zM7 19c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zM20 3H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1V4c0-.55-.45-1-1-1zM7 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"


@dataclass(frozen=True)
class Theme:
    dark: bool
    bg: str
    surface: str
    raised: str
    hover: str
    pressed: str
    line: str
    text: str
    text_dim: str
    text_faint: str
    good: str
    warn: str
    bad: str
    accent: str
    accent_hi: str    # accent-coloured text on the background
    accent_soft: str  # accent-tinted surface
    accent_ink: str   # text and icons on an accent-filled shape


def mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    a, b = QColor(a), QColor(b)
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def _luminance(color: QColor) -> float:
    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(color.redF()) + 0.7152 * lin(color.greenF()) + 0.0722 * lin(color.blueF())


def _for_light_mode(color: QColor) -> QColor:
    """Accents are chosen for a dark background; deepen them on a light one."""
    neutral = max(0.0, min(1.0, 1 - color.hslSaturationF() / 0.35))
    coloured = color.lightnessF() * 0.8
    inverted = max(0.07, 1.04 - color.lightnessF())
    return QColor.fromHslF(max(0.0, color.hslHueF()), color.hslSaturationF(),
                           coloured + (inverted - coloured) * neutral, 1.0)


def build(dark: bool, accent: str = DEFAULT_ACCENT) -> Theme:
    base = _DARK if dark else _LIGHT
    accent_c = QColor(accent) if dark else _for_light_mode(QColor(accent))
    hi = mix(accent_c, "#ffffff", 0.24) if dark else mix(accent_c, "#000000", 0.28)
    return Theme(
        dark=dark, **base,
        accent=accent_c.name(), accent_hi=hi.name(),
        accent_soft=mix(base["bg"], accent_c, 0.2).name(),
        accent_ink="#0c0d10" if _luminance(accent_c) > 0.22 else "#ffffff",
    )


def accent_for_hue(hue: float, saturation: float = 0.78) -> QColor:
    """A colour of the given hue (0..1) at the brightness all accents share.

    HSL lightness does not track perceived brightness across hues (yellow is
    far brighter than blue), so this solves for a fixed luminance instead.
    """
    hue -= int(hue)
    lo, hi = 0.2, 0.95
    for _ in range(18):
        mid = (lo + hi) / 2
        if _luminance(QColor.fromHslF(hue, saturation, mid, 1.0)) < _ACCENT_LUMINANCE:
            lo = mid
        else:
            hi = mid
    return QColor.fromHslF(hue, saturation, (lo + hi) / 2, 1.0)


def accent_from_frame(image: QImage) -> QColor:
    """The accent for one (thumbnail-sized) video frame: its dominant hue, or
    for a colourless frame a neutral from light grey (black) to white (bright)."""
    bins = 36
    weight = [0.0] * bins
    sat_sum = [0.0] * bins
    total = 0.0
    brightness = 0
    pixels = image.width() * image.height()
    if pixels == 0:
        return QColor(DEFAULT_ACCENT)
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            brightness += max(color.red(), color.green(), color.blue())
            h, s, v = color.hsvHueF(), color.hsvSaturationF(), color.valueF()
            # Colourful, bright pixels count most; near-grey ones not at all.
            if h < 0 or s < 0.18 or v < 0.2:
                continue
            w = s * s * v
            index = min(bins - 1, int(h * bins))
            weight[index] += w
            sat_sum[index] += w * s
            total += w
    # Too little colour to call: a few stray pixels should not pick the accent.
    if total < pixels * 0.012:
        level = 0.76 + 0.24 * brightness / (pixels * 255.0)
        return QColor.fromRgbF(level, level, level, 1.0)
    best = max(range(bins), key=lambda i: weight[i] + 0.6 * (weight[(i + 1) % bins] + weight[i - 1]))
    # Weighted circular mean over the winning bin and its neighbours.
    x = y = w = s = 0.0
    for d in range(-2, 3):
        i = (best + d) % bins
        angle = (i + 0.5) / bins * 2 * math.pi
        x += weight[i] * math.cos(angle)
        y += weight[i] * math.sin(angle)
        w += weight[i]
        s += sat_sum[i]
    hue = math.atan2(y, x) / (2 * math.pi) % 1.0
    saturation = s / w if w > 0 else 0.0
    # Keep it clearly coloured but never neon.
    return accent_for_hue(hue, max(0.55, min(0.9, 0.4 + saturation * 0.6)))


_current = build(True)
_accent = QColor(DEFAULT_ACCENT)  # as chosen; build() adapts it to light mode
_applied: tuple | None = None
_asset_dir: Path | None = None


def current() -> Theme:
    return _current


_system_palette_dark: bool | None = None
_scheme_is_live = False


def _on_scheme_changed(*_args) -> None:
    global _scheme_is_live
    _scheme_is_live = True


def system_dark(app: QApplication) -> bool:
    """Whether the desktop prefers dark.

    Some platform themes (qt6ct) report Light at startup on a dark desktop and
    only correct themselves with a change signal later. Until that signal has
    been seen, the platform palette we were started with also gets a vote.
    """
    global _system_palette_dark
    if _system_palette_dark is None:
        # First call, before our own palette replaces the platform's.
        _system_palette_dark = app.palette().color(QPalette.Window).lightness() < 128
        if hasattr(app.styleHints(), "colorSchemeChanged"):
            app.styleHints().colorSchemeChanged.connect(_on_scheme_changed)
    try:
        scheme = app.styleHints().colorScheme()
    except AttributeError:  # Qt < 6.5
        return _system_palette_dark
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light and _scheme_is_live:
        return False
    return _system_palette_dark


# -- icons ---------------------------------------------------------------------

def _svg(path: str, color: str) -> bytes:
    c = QColor(color)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<path fill="{c.name()}" fill-opacity="{c.alphaF():.3f}" d="{path}"/></svg>'
    ).encode()


@lru_cache(maxsize=512)
def _pixmap(path: str, color: str, size: int, dpr: float) -> QPixmap:
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(_svg(path, color))).render(painter, QRectF(pixmap.rect()))
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def icon_pixmap(path: str, color: str | QColor, size: int, dpr: float = 1.0) -> QPixmap:
    return _pixmap(path, QColor(color).name(QColor.HexArgb), int(size), float(dpr))


@lru_cache(maxsize=64)
def _icon(path: str, color: str, size: int) -> QIcon:
    result = QIcon()
    for dpr in (1.0, 2.0, 3.0):
        result.addPixmap(_pixmap(path, color, size, dpr))
    return result


def icon(path: str, color: str | QColor | None = None, size: int = 18) -> QIcon:
    """A QIcon for stock item views; custom widgets paint ``icon_pixmap`` directly."""
    return _icon(path, QColor(color or _current.text_dim).name(QColor.HexArgb), size)


def _assets(theme: Theme) -> dict[str, str]:
    """Arrow images the style sheet references (QSS takes only file URLs)."""
    global _asset_dir
    if _asset_dir is None:
        _asset_dir = Path(tempfile.mkdtemp(prefix="relay-desktop-theme-"))
        atexit.register(shutil.rmtree, _asset_dir, True)
    out = {}
    for name, path in (("down", Icons.chevron_down), ("up", Icons.chevron_up),
                       ("right", Icons.chevron_right)):
        # The colour is part of the name so a theme switch busts Qt's image cache.
        file = _asset_dir / f"{name}-{theme.text_dim[1:]}.svg"
        if not file.exists():
            file.write_bytes(_svg(path, theme.text_dim))
        out[name] = file.as_posix()
    return out


# -- palette + style sheet -------------------------------------------------------

def _palette(t: Theme) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.Window: t.bg, QPalette.WindowText: t.text,
        QPalette.Base: t.raised, QPalette.AlternateBase: t.accent_soft,
        QPalette.Text: t.text, QPalette.PlaceholderText: t.text_faint,
        QPalette.Button: t.hover, QPalette.ButtonText: t.text,
        QPalette.Highlight: t.accent, QPalette.HighlightedText: t.accent_ink,
        QPalette.Link: t.accent_hi, QPalette.Mid: t.line,
        QPalette.ToolTipBase: t.pressed, QPalette.ToolTipText: t.text,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(t.text_faint))
    return palette


def _style_sheet(t: Theme, a: dict[str, str]) -> str:
    # Accent colours are palette references (highlight, highlighted-text, link,
    # alternate-base), never literals: a palette change then recolours them
    # for free, where a new style sheet would re-polish every widget (~70 ms,
    # a visible hitch in the video when the accent follows the picture).
    return f"""
QWidget {{ font-size: 13px; }}
QToolTip {{ background: {t.pressed}; color: {t.text}; border: 0; padding: 5px 8px; font-size: 12px; }}
#sidebar {{ background: {t.surface}; }}
#trackPanel {{ background: {t.raised}; border: 1px solid {t.line}; border-radius: {RADIUS}px; }}
#sidebarFooter {{ border-top: 1px solid {t.line}; }}
QLabel#wordmark {{ font-size: 16px; font-weight: 600; }}
QLabel#heading {{ font-size: 17px; font-weight: 600; }}
QLabel#panelTitle {{ font-size: 19px; font-weight: 600; }}
QLabel#nowTitle {{ font-size: 14px; font-weight: 600; }}
QLabel[role="dim"] {{ color: {t.text_dim}; font-size: 12px; }}
QLabel[role="faint"] {{ color: {t.text_faint}; font-size: 12px; }}

QLineEdit {{ background: {t.raised}; border: 1px solid transparent; border-radius: {RADIUS_SMALL}px;
    padding: 0 10px; min-height: 30px; selection-background-color: palette(highlight); selection-color: palette(highlighted-text); }}
QLineEdit:hover {{ background: {t.hover}; }}
QLineEdit:focus {{ background: {t.hover}; border-color: palette(highlight); }}
QLineEdit[pill="true"] {{ border-radius: 17px; min-height: 32px; padding: 0 14px; }}

QPushButton {{ background: {t.hover}; color: {t.text}; border: 0; border-radius: {RADIUS_SMALL}px;
    padding: 0 14px; min-height: 34px; font-weight: 600; }}
QPushButton:hover, QPushButton:pressed {{ background: {t.pressed}; }}
QPushButton:disabled {{ color: {t.text_faint}; }}
QPushButton[primary="true"] {{ background: palette(highlight); color: palette(highlighted-text); }}
QPushButton[primary="true"]:hover {{ background: palette(link); }}

QComboBox {{ background: {t.raised}; border: 1px solid transparent; border-radius: {RADIUS_SMALL}px;
    padding: 0 10px; min-height: 30px; }}
QComboBox:hover, QComboBox:on {{ background: {t.hover}; }}
QComboBox:focus {{ border-color: palette(highlight); }}
QComboBox:disabled {{ color: {t.text_faint}; }}
QComboBox::drop-down {{ border: 0; width: 26px; }}
QComboBox::down-arrow {{ image: url({a['down']}); width: 16px; height: 16px; }}
QComboBox QAbstractItemView {{ background: {t.raised}; border: 1px solid {t.line}; padding: 4px; outline: 0;
    selection-background-color: {t.hover}; selection-color: {t.text}; }}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 0 8px; border-radius: {RADIUS_SMALL}px; }}

QAbstractSpinBox {{ background: {t.raised}; border: 1px solid transparent; border-radius: {RADIUS_SMALL}px;
    padding: 0 8px; min-height: 30px; selection-background-color: palette(highlight); selection-color: palette(highlighted-text); }}
QAbstractSpinBox:hover {{ background: {t.hover}; }}
QAbstractSpinBox:focus {{ border-color: palette(highlight); }}
QAbstractSpinBox:disabled {{ color: {t.text_faint}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ border: 0; background: transparent; width: 20px; }}
QAbstractSpinBox::up-arrow {{ image: url({a['up']}); width: 12px; height: 12px; }}
QAbstractSpinBox::down-arrow {{ image: url({a['down']}); width: 12px; height: 12px; }}

QTreeView {{ background: transparent; border: 0; outline: 0; show-decoration-selected: 1; }}
QTreeView::item {{ min-height: 30px; padding: 0 4px; border: 0; color: {t.text_dim}; }}
QTreeView::item:hover {{ background: {t.hover}; color: {t.text}; }}
QTreeView::item:selected {{ background: palette(alternate-base); color: {t.text}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:hover {{ background: {t.hover}; }}
QTreeView::branch:selected {{ background: palette(alternate-base); }}
QTreeView::branch:has-children:closed {{ image: url({a['right']}); }}
QTreeView::branch:has-children:open {{ image: url({a['down']}); }}

QScrollArea {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle {{ background: {t.pressed}; border-radius: 2px; }}
QScrollBar::handle:vertical {{ min-height: 24px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ min-width: 24px; margin: 2px 0; }}
QScrollBar::handle:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: transparent; color: {t.text_dim}; border: 0; border-radius: {RADIUS_SMALL}px;
    padding: 6px 16px; margin: 0 2px 6px 2px; font-weight: 600; }}
QTabBar::tab:hover {{ background: {t.hover}; color: {t.text}; }}
QTabBar::tab:selected {{ background: palette(alternate-base); color: palette(link); }}

QSplitter::handle {{ background: {t.line}; }}
#settingsPanel {{ background: {t.surface}; border-left: 1px solid {t.line}; }}
#settingsPage {{ background: {t.surface}; }}
#settingsPage QLabel {{ color: {t.text_dim}; }}
QGroupBox {{ border: 0; border-top: 1px solid {t.line}; margin-top: 34px; padding-top: 4px;
    font-size: 14px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 10px 0 6px 0; color: {t.text}; }}

QMenu {{ background: {t.raised}; border: 1px solid {t.line}; border-radius: {RADIUS}px; padding: 6px; }}
QMenu::item {{ padding: 8px 14px; border-radius: {RADIUS_SMALL}px; color: {t.text}; }}
QMenu::item:selected {{ background: {t.hover}; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMessageBox {{ background: {t.raised}; }}
"""


def accent_source() -> QColor:
    return QColor(_accent)


def set_accent(app: QApplication, color: str | QColor) -> Theme:
    """Change the accent. Cheap enough to call on every step of a glide: the
    custom widgets read ``current()`` as they repaint, and the style sheet's
    accent colours are palette references."""
    global _current, _accent
    _accent = QColor(color)
    _current = build(_current.dark, _accent.name())
    app.setPalette(_palette(_current))
    return _current


def apply_theme(app: QApplication, mode: str = "auto", accent: str | QColor | None = None) -> Theme:
    """Install the palette and style sheet for ``mode`` ("auto", "dark", "light")."""
    global _accent
    if accent is not None:
        _accent = QColor(accent)
    return _install(app, system_dark(app) if mode == "auto" else mode != "light")


def _install(app: QApplication, dark: bool) -> Theme:
    global _current, _applied
    key = (id(app), dark)
    if _applied == key:
        return set_accent(app, _accent)
    _current = build(dark, _accent.name())
    # With a style sheet installed Qt stops handing a parent's palette down to
    # its children unless asked; the fullscreen overlay relies on that.
    QApplication.setAttribute(Qt.AA_UseStyleSheetPropagationInWidgetStyles, True)
    if app.style().objectName().lower() != "fusion":
        # One predictable base for the style sheet on every platform theme.
        app.setStyle("Fusion")
    app.setPalette(_palette(_current))
    app.setStyleSheet(_style_sheet(_current, _assets(_current)))
    _applied = key
    return _current


def style_menu(menu) -> None:
    """Let a popup menu draw the rounded card from the style sheet."""
    menu.setWindowFlag(Qt.FramelessWindowHint)
    menu.setWindowFlag(Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground)
