"""Custom-painted controls for the flat theme (see ``theme.py``).

Each widget keeps the stock Qt API of the class it stands in for
(QAbstractButton, QCheckBox, QSlider, QLabel), so window logic and tests keep
driving them the usual way; only painting and hover/press motion are ours.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QToolTip,
    QWidget,
)

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
        self._hover = _Fade(self)
        self._press = _Fade(self)
        self.pressed.connect(lambda: self._press.go(1.0))
        self.released.connect(lambda: self._press.go(0.0))
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
        # Centred on the widget's true centre and kept half a pixel inside it,
        # so the antialiased edge of the disc is never clipped.
        rect = QRectF(0, 0, min(hint.width(), self.width()) - 1, min(hint.height(), self.height()) - 1)
        rect.moveCenter(QRectF(self.rect()).center())
        if self._press.value > 0:
            # Shrink around the centre, like a pressed key.
            scale = 1 - 0.08 * self._press.value
            painter.translate(rect.center())
            painter.scale(scale, scale)
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
            # Ticks are exactly as tall as the track, so they read as notches
            # in the bar: dark over the played part, light over the rest.
            ahead = QColor(self.palette().color(QPalette.WindowText))
            ahead.setAlpha(150)
            played = QColor(t.accent_ink)
            played.setAlpha(170)
            span = self.maximum() - self.minimum()
            painter.setRenderHint(QPainter.Antialiasing, False)
            for fraction in self._marks:
                mx = self._x_for(round(self.minimum() + fraction * span))
                if 2 <= mx <= self.width() - 2:
                    painter.fillRect(QRectF(round(mx) - 1, track.top(), 2, track.height()),
                                     played if mx < x else ahead)
            painter.setRenderHint(QPainter.Antialiasing, True)
        if engaged > 0:
            radius = 7 * engaged * (1.15 if self.isSliderDown() else 1.0)
            cx = max(radius + 0.5, min(self.width() - radius - 0.5, x))
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
        self._busy = False
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._spin)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_busy(self, busy: bool) -> None:
        """Swap the icon for a progress ring while something is being prepared."""
        self._busy = busy
        if busy and self.isVisible():
            self._timer.start()
        elif not busy:
            self._timer.stop()
        self.update()

    def _spin(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event) -> None:
        if self._busy:
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

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
        if self._busy:
            pen = QPen(QColor(t.accent), 3.5)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(tile.adjusted(18, 18, -18, -18), -self._angle * 16, -270 * 16)
        else:
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


def emphasized() -> QEasingCurve:
    """ "Emphasized decelerate": quick start, long soft landing."""
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.2, 0.0), QPointF(0.0, 1.0), QPointF(1.0, 1.0))
    return curve


class Reveal(QObject):
    """Animated show/hide for a child panel that floats over the window.

    Showing glides the panel in from ``offset`` (and fades it, if asked) with
    a long soft landing; hiding is quick. The panel is really hidden only
    when the exit finishes, and its final geometry can be re-targeted while
    shown (window resizes).
    """

    def __init__(self, widget: QWidget, *, offset: QPoint = QPoint(0, 10), fade: bool = True,
                 show_ms: int = theme.SLOW, hide_ms: int = theme.FAST, easing=None):
        super().__init__(widget)
        self._widget = widget
        self._offset = offset
        self._fade = fade
        self._show_ms, self._hide_ms = show_ms, hide_ms
        self._easing = easing or emphasized()
        self._geometry = widget.geometry()
        self._t = 0.0
        self._target = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.valueChanged.connect(self._apply)
        self._animation.finished.connect(self._finished)

    @property
    def shown(self) -> bool:
        """Whether the panel is (heading for) shown."""
        return self._target > 0

    def retarget(self, geometry: QRect) -> None:
        self._geometry = QRect(geometry)
        self._place()

    def show(self, geometry: QRect) -> None:
        self._geometry = QRect(geometry)
        self._run(1.0, self._show_ms, self._easing)
        self._widget.show()
        self._widget.raise_()

    def hide(self) -> None:
        if not self._widget.isVisible():
            self._target = self._t = 0.0
            return
        self._run(0.0, self._hide_ms, QEasingCurve(QEasingCurve.InCubic))

    def show_now(self, geometry: QRect) -> None:
        self._animation.stop()
        self._geometry = QRect(geometry)
        self._target = self._t = 1.0
        self._place()
        self._widget.show()
        self._widget.raise_()

    def hide_now(self) -> None:
        self._animation.stop()
        self._target = self._t = 0.0
        self._clear_effect()
        self._widget.hide()

    def _run(self, target: float, duration: int, easing) -> None:
        self._animation.stop()
        self._target = target
        if self._t == target:
            # Already there (e.g. dismissed before the entrance moved at all).
            self._place()
            self._finished()
            return
        self._animation.setDuration(max(1, round(duration * abs(target - self._t))))
        self._animation.setEasingCurve(easing)
        self._animation.setStartValue(self._t)
        self._animation.setEndValue(target)
        self._place()
        self._animation.start()

    def _apply(self, value) -> None:
        self._t = float(value)
        self._place()

    def _place(self) -> None:
        away = 1.0 - self._t
        self._widget.setGeometry(QRect(
            self._geometry.topLeft() + QPoint(round(self._offset.x() * away), round(self._offset.y() * away)),
            self._geometry.size(),
        ))
        if self._fade and 0.0 < self._t < 1.0:
            effect = self._widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(self._widget)
                self._widget.setGraphicsEffect(effect)
            effect.setOpacity(self._t)
        elif self._fade:
            self._clear_effect()

    def _clear_effect(self) -> None:
        # Only ever present mid-transition: a permanent effect would route
        # every repaint of the panel through an offscreen pixmap.
        if self._widget.graphicsEffect() is not None:
            self._widget.setGraphicsEffect(None)

    def _finished(self) -> None:
        self._t = self._target
        self._clear_effect()
        if self._target == 0.0:
            self._widget.hide()


class SlideSlot(QWidget):
    """Holds one widget so it can slide out of view instead of being squeezed.

    While an extent is held, the widget keeps that width (or height) and stays
    anchored to the slot's far edge, so narrowing the slot moves it off the
    near edge with its layout intact. With nothing held it simply fills the slot.
    ``set_hidden`` moves it off the near edge without changing the slot's size,
    so the layout around the slot (the video) stays where it is.
    """

    def __init__(self, child: QWidget, orientation=Qt.Horizontal, parent=None):
        super().__init__(parent)
        self._child = child
        self._orientation = orientation
        self._held: int | None = None
        self._hidden = 0.0
        child.setParent(self)

    def hold(self, extent: int | None) -> None:
        self._held = extent
        self.updateGeometry()
        self._place()

    def set_hidden(self, fraction: float) -> None:
        """Shift the widget ``fraction`` of its extent past the near edge."""
        self._hidden = fraction
        self._place()

    def sizeHint(self) -> QSize:
        return self._child.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0) if self._held is not None else self._child.minimumSizeHint()

    def resizeEvent(self, event) -> None:
        self._place()
        super().resizeEvent(event)

    def _place(self) -> None:
        if self._orientation == Qt.Horizontal:
            width = max(self.width(), self._held or 0)
            shift = round(width * self._hidden)
            self._child.setGeometry(self.width() - width - shift, 0, width, self.height())
        else:
            height = max(self.height(), self._held or 0)
            shift = round(height * self._hidden)
            self._child.setGeometry(0, self.height() - height - shift, self.width(), height)


class AutoHideButton(QPushButton):
    """Push button that exists only while it can be used (the Stop button)."""

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            self.setVisible(self.isEnabled())


class WheelGuard(QObject):
    """Keeps the wheel from changing a combo box or spin box under the pointer.

    Scrolling a panel should scroll the panel; a value changes only through a
    deliberate click. The wheel event is handed to ``scroll_target`` (the
    panel's viewport) when there is one, otherwise swallowed.
    """

    def __init__(self, parent: QWidget, scroll_target: QWidget | None = None):
        super().__init__(parent)
        self._scroll_target = scroll_target
        for child in parent.findChildren(QWidget):
            if isinstance(child, (QComboBox, QAbstractSpinBox, QSlider)):
                # StrongFocus drops WheelFocus: a wheel turn no longer focuses it.
                child.setFocusPolicy(Qt.StrongFocus)
                child.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Wheel:
            if self._scroll_target is not None:
                QApplication.sendEvent(self._scroll_target, event)
            return True
        return False


class SegmentedSwitch(QWidget):
    """Pill of mutually exclusive options with a sliding accent highlight."""

    selected = Signal(str)

    def __init__(self, options: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._options = options  # (label, key)
        self._index = 0
        self._slide = _Fade(self, theme.SLOW)
        self._slide.setEasingCurve(emphasized())
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(76 * len(self._options), 32)

    def current(self) -> str:
        return self._options[self._index][1]

    def set_current(self, key: str) -> None:
        for index, (_label, value) in enumerate(self._options):
            if value == key:
                self._index = index
                self._slide.go(float(index))
                self.update()

    def mousePressEvent(self, event) -> None:
        index = int(event.position().x() / max(1.0, self.width() / len(self._options)))
        index = max(0, min(len(self._options) - 1, index))
        if index != self._index:
            self.set_current(self._options[index][1])
            self.selected.emit(self._options[index][1])

    def paintEvent(self, _event) -> None:
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(t.hover))
        painter.drawRoundedRect(QRectF(self.rect()), 16, 16)
        cell = (self.width() - 6) / len(self._options)
        painter.setBrush(QColor(t.accent))
        painter.drawRoundedRect(QRectF(3 + self._slide.value * cell, 3, cell, self.height() - 6), 13, 13)
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        for index, (label, _key) in enumerate(self._options):
            near = max(0.0, 1.0 - abs(self._slide.value - index))
            painter.setPen(mix(t.text_dim, t.accent_ink, near))
            painter.drawText(QRectF(3 + index * cell, 0, cell, self.height()), Qt.AlignCenter, label)
        painter.end()


class AccentPicker(QWidget):
    """Accent colour: follow the video ("auto"), a preset, or any hue.

    A row with the Auto chip and the preset swatches, and under it a hue strip
    to click or drag along.
    """

    picked = Signal(str)  # "auto" or "#rrggbb"

    _SWATCH = 28
    _STRIP_Y = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = "auto"
        self._hover = None  # "auto", a preset index, or "strip"
        self._hover_fade = _Fade(self)
        self._dragging = False
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(64)
        self.setMinimumWidth(300)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self.update()

    @property
    def custom(self) -> bool:
        return self._value != "auto" and self._value not in theme.ACCENT_PRESETS

    # -- geometry ---------------------------------------------------------------

    def _chip_rect(self) -> QRectF:
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.DemiBold)
        return QRectF(0, 0, QFontMetricsF(font).horizontalAdvance("Auto") + 26, self._SWATCH)

    def _swatch_rect(self, index: int) -> QRectF:
        chip = self._chip_rect()
        count = len(theme.ACCENT_PRESETS)
        spacing = min(9.0, max(1.0, (self.width() - chip.width() - count * self._SWATCH) / count))
        return QRectF(chip.right() + spacing + index * (self._SWATCH + spacing), 0,
                      self._SWATCH, self._SWATCH)

    def _strip_rect(self) -> QRectF:
        return QRectF(0, self._STRIP_Y, self.width(), 14)

    def _target_at(self, pos):
        if self._chip_rect().contains(pos):
            return "auto"
        for index in range(len(theme.ACCENT_PRESETS)):
            if self._swatch_rect(index).contains(pos):
                return index
        if self._strip_rect().adjusted(0, -6, 0, 6).contains(pos):
            return "strip"
        return None

    # -- interaction ------------------------------------------------------------

    def _pick_hue(self, x: float) -> None:
        hue = max(0.0, min(0.999, x / max(1, self.width())))
        self._value = theme.accent_for_hue(hue).name()
        self.update()
        self.picked.emit(self._value)

    def mousePressEvent(self, event) -> None:
        target = self._target_at(event.position())
        if target == "strip":
            self._dragging = True
            self._pick_hue(event.position().x())
        elif target is not None:
            self._value = "auto" if target == "auto" else theme.ACCENT_PRESETS[target]
            self.update()
            self.picked.emit(self._value)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._pick_hue(event.position().x())
            return
        target = self._target_at(event.position())
        if target != self._hover:
            self._hover = target
            self._hover_fade._value = 0.0
            self._hover_fade.go(1.0 if target is not None else 0.0)
            self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._dragging = False

    def leaveEvent(self, event) -> None:
        self._hover = None
        self._hover_fade.go(0.0)
        self.update()
        super().leaveEvent(event)

    # -- painting ---------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        t = theme.current()
        glow = self._hover_fade.value
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        chip = self._chip_rect().adjusted(1, 1, -1, -1)
        auto = self._value == "auto"
        fill = QColor(t.accent_soft) if auto else mix(t.hover, t.pressed, glow if self._hover == "auto" else 0.0)
        painter.setPen(QPen(QColor(t.accent), 2) if auto else Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(t.accent_hi if auto else t.text))
        painter.drawText(chip, Qt.AlignCenter, "Auto")

        for index, preset in enumerate(theme.ACCENT_PRESETS):
            cell = self._swatch_rect(index)
            chosen = self._value == preset
            if chosen:
                painter.setPen(QPen(QColor(preset), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(cell.adjusted(1, 1, -1, -1))
            radius = 9.0 * (1.0 + (0.2 * glow if self._hover == index and not chosen else 0.0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(preset))
            painter.drawEllipse(cell.center(), radius, radius)

        strip = self._strip_rect()
        height = 6 + 2 * (glow if self._hover == "strip" or self._dragging else 0.0)
        bar = QRectF(0, strip.center().y() - height / 2, strip.width(), height)
        gradient = QLinearGradient(bar.topLeft(), bar.topRight())
        for step in range(7):
            gradient.setColorAt(step / 6, theme.accent_for_hue(min(0.999, step / 6)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(bar, height / 2, height / 2)
        if self.custom:
            color = QColor(self._value)
            x = max(7.0, min(self.width() - 7.0, max(0.0, color.hslHueF()) * self.width()))
            painter.setPen(QPen(QColor(t.text), 2))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x, strip.center().y()), 6, 6)
        painter.end()


class PlayerOverlay(QWidget):
    """Feedback drawn over the video: a busy ring while it loads or seeks,
    and a play/pause mark that flashes on toggle. Never takes the mouse."""

    _DISC = 84

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._busy = _Fade(self, theme.NORMAL)
        self._busy_on = False
        self._angle = 0
        self._spin = QTimer(self)
        self._spin.setInterval(30)
        self._spin.timeout.connect(self._step)
        self._flash = QVariantAnimation(self)
        self._flash.setDuration(520)
        self._flash.setStartValue(0.0)
        self._flash.setEndValue(1.0)
        self._flash.valueChanged.connect(lambda _v: self.update(self._centre()))
        self._flash_icon = Icons.pause

    def _centre(self) -> QRect:
        side = round(self._DISC * 1.3) + 4
        return QRect((self.width() - side) // 2, (self.height() - side) // 2, side, side)

    def _step(self) -> None:
        self._angle = (self._angle + 12) % 360
        if not self._busy_on and self._busy.value <= 0:
            self._spin.stop()
        self.update(self._centre())

    @property
    def busy(self) -> bool:
        return self._busy_on

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy_on:
            return
        self._busy_on = busy
        self._busy.go(1.0 if busy else 0.0)
        if busy and self.isVisible():
            self._spin.start()

    def flash(self, icon: str) -> None:
        """Briefly show ``icon`` (the state just entered) in the middle."""
        self._flash_icon = icon
        self._flash.stop()
        if self.isVisible():
            self._flash.start()

    def hideEvent(self, event) -> None:
        self._spin.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._busy_on:
            self._spin.start()
        super().showEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        centre = QPointF(self.width() / 2, self.height() / 2)
        busy = self._busy.value
        if busy > 0:
            scrim = QColor(theme.SCRIM)
            scrim.setAlphaF(0.7 * busy)
            painter.setPen(Qt.NoPen)
            painter.setBrush(scrim)
            painter.drawEllipse(centre, 36, 36)
            ring = QColor(theme.current().accent)
            ring.setAlphaF(busy)
            pen = QPen(ring, 4)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(QRectF(centre.x() - 18, centre.y() - 18, 36, 36), -self._angle * 16, -270 * 16)
        if self._flash.state() == QVariantAnimation.Running:
            k = float(self._flash.currentValue())
            opacity = 0.95 * (1 - k ** 3)
            scale = 0.8 + 0.45 * (1 - (1 - k) ** 3)
            painter.setOpacity(opacity)
            radius = self._DISC / 2 * scale
            scrim = QColor(theme.SCRIM)
            scrim.setAlphaF(0.7)
            painter.setPen(Qt.NoPen)
            painter.setBrush(scrim)
            painter.drawEllipse(centre, radius, radius)
            size = 44 * scale
            nudge = 3 * scale if self._flash_icon == Icons.play else 0
            pixmap = theme.icon_pixmap(self._flash_icon, theme.SCRIM_TEXT, 44, self.devicePixelRatioF() * 1.3)
            painter.drawPixmap(QRectF(centre.x() - size / 2 + nudge, centre.y() - size / 2, size, size),
                               pixmap, QRectF(pixmap.rect()))
        painter.end()
