"""Custom-painted controls for the flat theme (see ``theme.py``).

Each widget keeps the stock Qt API of the class it stands in for
(QAbstractButton, QCheckBox, QSlider, QLabel), so window logic and tests keep
driving them the usual way; only painting and hover/press motion are ours.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, QEasingCurve, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import QAbstractButton, QCheckBox, QFrame, QLabel, QSlider, QStyle, QToolTip, QWidget

from . import theme
from .theme import Icons, mix


def tabular(font: QFont) -> QFont:
    """Fixed-width digits, so a running clock does not jitter."""
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):  # Qt < 6.7
        pass
    return font


class _Fade(QVariantAnimation):
    """0..1 value that glides towards its target and repaints its widget."""

    def __init__(self, widget: QWidget, duration: int = theme.FAST):
        super().__init__(widget)
        self.setDuration(duration)
        self.setEasingCurve(QEasingCurve.OutCubic)
        self.setStartValue(0.0)
        self.setEndValue(0.0)
        self._value = 0.0
        self._widget = widget
        self.valueChanged.connect(self._on_value)

    def _on_value(self, value) -> None:
        self._value = float(value)
        self._widget.update()

    @property
    def value(self) -> float:
        return self._value

    def go(self, target: float) -> None:
        self.stop()
        if not self._widget.isVisible():
            self._value = target
            return
        self.setStartValue(self._value)
        self.setEndValue(float(target))
        self.start()


class IconButton(QAbstractButton):
    """Round, flat icon button with hover and press feedback.

    Without an icon it draws its text as a pill instead (the skip buttons).
    """

    def __init__(self, icon: str = "", *, size: int = 36, icon_size: int = 20,
                 filled: bool = False, accent_checked: bool = True, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._size = size
        self._icon_size = icon_size
        self._filled = filled  # solid accent disc (primary action)
        self._accent_checked = accent_checked
        self._over_video = False
        self._hover = _Fade(self)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover)

    def set_icon(self, icon: str) -> None:
        if icon != self._icon:
            self._icon = icon
            self.update()

    def sizeHint(self) -> QSize:
        if self._icon or not self.text():
            return QSize(self._size, self._size)
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 22, self._size)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def enterEvent(self, event) -> None:
        self._hover.go(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover.go(0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        hover = self._hover.value if self.isEnabled() else 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.isEnabled():
            painter.setOpacity(0.35)
        hint = self.sizeHint()
        rect = QRectF(0, 0, hint.width(), hint.height())
        rect.moveCenter(QPointF(self.rect().center()) + QPointF(0.5, 0.5))
        if self.isDown():
            # Shrink around the centre, like a pressed key.
            painter.translate(rect.center())
            painter.scale(0.92, 0.92)
            painter.translate(-rect.center())
        painter.setPen(Qt.NoPen)
        if self._filled:
            painter.setBrush(mix(t.accent, t.accent_hi, hover))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        elif hover > 0 or self.hasFocus():
            disc = QColor(t.pressed if self.isDown() else t.hover)
            disc.setAlphaF(max(hover, 1.0 if self.hasFocus() else 0.0))
            painter.setBrush(disc)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if self._filled:
            color = QColor(t.accent_ink)
        elif self.isChecked() and self._accent_checked:
            color = QColor(t.accent)
        else:
            color = mix(t.text_dim, t.text, hover)
        if self._icon:
            dpr = self.devicePixelRatioF()
            target = QRectF(0, 0, self._icon_size, self._icon_size)
            target.moveCenter(rect.center())
            pixmap = theme.icon_pixmap(self._icon, color, self._icon_size, dpr)
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        else:
            font = tabular(self.font())
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignCenter, self.text())
        painter.end()


class FlatSwitch(QCheckBox):
    """QCheckBox drawn as a label with a toggle switch on the right."""

    _TRACK_W, _TRACK_H = 40, 22

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._knob = _Fade(self, theme.NORMAL)
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(lambda on: self._knob.go(1.0 if on else 0.0))

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 20 + self._TRACK_W,
                     max(34, metrics.height() + 10))

    def minimumSizeHint(self) -> QSize:
        return QSize(self._TRACK_W + 60, self.sizeHint().height())

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        k = self._knob.value
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        track = QRectF(self.width() - self._TRACK_W, (self.height() - self._TRACK_H) / 2,
                       self._TRACK_W, self._TRACK_H)
        painter.setPen(Qt.NoPen)
        painter.setBrush(mix(t.pressed, t.accent, k))
        painter.drawRoundedRect(track, self._TRACK_H / 2, self._TRACK_H / 2)
        painter.setBrush(mix(t.text_dim, t.accent_ink, k))
        x = track.left() + 3 + (self._TRACK_W - 22) * k
        painter.drawEllipse(QRectF(x, track.top() + 3, 16, 16))
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(t.accent), 1))
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 13, 13)
        painter.setPen(self.palette().color(QPalette.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, int(track.left()) - 12)
        painter.drawText(QRectF(0, 0, track.left() - 12, self.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.end()


class FlatSlider(QSlider):
    """Thin horizontal slider: the track thickens and a handle appears on hover."""

    def __init__(self, *args, accent: bool = True) -> None:
        super().__init__(*args)
        self._accent = accent
        self._engage = _Fade(self)
        self._marks: list[float] = []
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover)
        self.setFixedHeight(20)
        self.sliderPressed.connect(lambda: self._engage.go(1.0))
        self.sliderReleased.connect(lambda: self._engage.go(1.0 if self.underMouse() else 0.0))

    def _x_for(self, value: int) -> float:
        # Same mapping as a groove click, so the handle sits under the pointer.
        return QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), value, self.width())

    def enterEvent(self, event) -> None:
        self._engage.go(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self.isSliderDown():
            self._engage.go(0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        engaged = self._engage.value if self.isEnabled() else 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        height = 4 + 2 * engaged
        track = QRectF(0, (self.height() - height) / 2, self.width(), height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(t.pressed))
        painter.drawRoundedRect(track, height / 2, height / 2)
        x = self._x_for(self.sliderPosition())
        if self.isEnabled():
            fill = QRectF(track)
            fill.setWidth(max(height, x))
            rest = QColor(t.accent if self._accent else t.text_dim)
            painter.setBrush(mix(rest, t.accent_hi, engaged))
            painter.drawRoundedRect(fill, height / 2, height / 2)
        if self._marks:
            mark = QColor(self.palette().color(QPalette.WindowText))
            mark.setAlpha(150)
            span = self.maximum() - self.minimum()
            for fraction in self._marks:
                mx = self._x_for(round(self.minimum() + fraction * span))
                painter.fillRect(QRectF(mx - 1, self.height() / 2 - 4, 2, 8), mark)
        if engaged > 0:
            radius = 7 * engaged * (1.15 if self.isSliderDown() else 1.0)
            cx = max(7.0, min(self.width() - 7.0, x))
            painter.setBrush(QColor(t.text))
            painter.drawEllipse(QPointF(cx, self.height() / 2), radius, radius)
        painter.end()


class VolumeSlider(FlatSlider):
    """Volume slider: a groove click jumps there and starts a drag."""

    def __init__(self, *args) -> None:
        super().__init__(*args, accent=False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.setSliderPosition(QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), round(event.position().x()), self.width()))
        super().mousePressEvent(event)


class Spinner(QWidget):
    """Indeterminate progress ring."""

    def __init__(self, size: int = 18, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._step)
        self.setFixedSize(size, size)

    def _step(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = max(1.5, self.width() / 8)
        pen = QPen(QColor(theme.current().accent), width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        inset = width / 2 + 1
        painter.drawArc(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset),
                        -self._angle * 16, -270 * 16)
        painter.end()


class ElideLabel(QLabel):
    """Single-line label that elides instead of widening its layout.

    The text colour is the palette's WindowText (optionally dimmed), so the
    label follows whatever palette its container carries.
    """

    def __init__(self, text: str = "", *, dim: float = 1.0, elide=Qt.ElideRight, parent=None):
        super().__init__(text, parent)
        self._dim = dim
        self._elide = elide

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        color = QColor(self.palette().color(QPalette.WindowText))
        color.setAlphaF(color.alphaF() * self._dim)
        painter.setPen(color)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), self._elide, rect.width())
        painter.drawText(rect, int(self.alignment()) | Qt.TextSingleLine, text)
        painter.end()


class ControlBar(QWidget):
    """Backdrop of the transport bar.

    Docked, it is the theme's surface with a hairline on top. As the
    fullscreen overlay it paints its own palette's Window colour instead, so
    the labels on it (which follow that same palette) stay readable. Painted
    here rather than by a style sheet rule, which would overwrite the palette.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.overlay = False

    def paintEvent(self, _event) -> None:
        t = theme.current()
        painter = QPainter(self)
        if self.overlay:
            painter.fillRect(self.rect(), self.palette().color(QPalette.Window))
        else:
            painter.fillRect(self.rect(), QColor(t.surface))
            painter.fillRect(0, 0, self.width(), 1, QColor(t.line))
        painter.end()


class SidePanel(QFrame):
    """Sheet that floats over the window's right edge (the settings panel).

    An overlay rather than a dock: a dock adds its width to the window's
    minimum size, which a tiling compositor cannot grant, so the window
    would grow past its tile and be clipped.
    """

    visibilityChanged = Signal(bool)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibilityChanged.emit(False)


class Logo(QWidget):
    """The accent tile with a play mark that heads the sidebar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)

    def paintEvent(self, _event) -> None:
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(t.accent))
        painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        pixmap = theme.icon_pixmap(Icons.play, t.accent_ink, 18, self.devicePixelRatioF())
        painter.drawPixmap(QRectF(6, 5, 18, 18), pixmap, QRectF(pixmap.rect()))
        painter.end()


class StatusDot(QWidget):
    """Small coloured dot beside the connection label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = "text_faint"
        self.setFixedSize(8, 8)

    def set_state(self, name: str) -> None:
        self._color = name
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(getattr(theme.current(), self._color)))
        painter.drawEllipse(QRectF(self.rect()))
        painter.end()


class IdleHint(QWidget):
    """Guidance shown over the empty video area: icon tile, heading, body.

    ``setText`` takes "Heading\\n\\nBody" so callers stay plain-text.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title = ""
        self._body = ""
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def setText(self, text: str) -> None:
        self._title, _, self._body = text.partition("\n\n")
        self.update()

    def text(self) -> str:
        return f"{self._title}\n\n{self._body}" if self._body else self._title

    def paintEvent(self, _event) -> None:
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        width = min(460, self.width() - 32)
        if width <= 0:
            return
        title_font = QFont(self.font())
        title_font.setPixelSize(22)
        title_font.setWeight(QFont.DemiBold)
        body_font = QFont(self.font())
        body_font.setPixelSize(14)
        painter.setFont(body_font)
        flags = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
        body_rect = painter.boundingRect(QRectF(0, 0, width, 400), flags, self._body)
        total = 64 + 18 + 30 + 10 + body_rect.height()
        left = (self.width() - width) / 2
        y = max(0.0, (self.height() - total) / 2 - 10)
        # The video area is black in both modes, so the tile and text use the
        # dark palette's tones regardless of the theme.
        tile = QRectF((self.width() - 64) / 2, y, 64, 64)
        painter.setPen(Qt.NoPen)
        painter.setBrush(mix(theme.SCRIM, t.accent, 0.2))
        painter.drawRoundedRect(tile, 18, 18)
        pixmap = theme.icon_pixmap(Icons.movie, t.accent, 32, self.devicePixelRatioF())
        painter.drawPixmap(tile.adjusted(16, 16, -16, -16), pixmap, QRectF(pixmap.rect()))
        y += 64 + 18
        painter.setFont(title_font)
        painter.setPen(QColor(theme.SCRIM_TEXT))
        painter.drawText(QRectF(left, y, width, 30), Qt.AlignHCenter | Qt.AlignVCenter, self._title)
        y += 30 + 10
        painter.setFont(body_font)
        painter.setPen(QColor(theme.SCRIM_TEXT_DIM))
        painter.drawText(QRectF(left, y, width, body_rect.height() + 4), flags, self._body)
        painter.end()


def show_slider_tip(slider: QSlider, x: float, text: str) -> None:
    """Small time bubble above a slider at pointer position ``x``."""
    QToolTip.showText(slider.mapToGlobal(QPointF(x, -34).toPoint()), text, slider)
