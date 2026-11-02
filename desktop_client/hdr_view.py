"""HDR output: the video on a native surface of its own.

Qt Widgets compose every window in SDR on every platform (QOpenGLWidget
included), so through the player widget mpv can only tone-map HDR. Real HDR
needs a surface the platform shows as HDR: a native child window placed over
the player widget (``HdrPresenter``), PQ-tagged on Wayland (Qt >= 6.10.2 sends
the format's colour space as a wp_color_management_v1 image description). mpv
renders into it through the same render API. The player widget underneath
keeps everything else: the layout, input (the surface is transparent for
input), focus and the overlays.

The overlays (fullscreen control bar, track and info cards, settings sheet,
busy ring) are ordinary widgets of the main window, which the native surface
covers. They are drawn again over the video here: every widget stacked above
the player is rendered into an image whenever it repaints
(``OverlayMirror``) and composited over the video, converted from sRGB at the
display's SDR white to PQ, as the compositor shows the rest of the window.
The real widgets underneath still take the pointer and the keys, so their
behaviour does not change.

Everything here runs inside Qt virtuals (paintGL, eventFilter): an exception
escaping one is a native crash, not a traceback, so each catches its own.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColorSpace, QImage, QPainter, QRegion, QSurfaceFormat, QVector4D, QWindow,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject, QOpenGLWindow,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

_GL_FRAMEBUFFER = 0x8D40
_GL_READ_FRAMEBUFFER = 0x8CA8
_GL_DRAW_FRAMEBUFFER = 0x8CA9
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_LINEAR = 0x2601
_GL_SCISSOR_TEST = 0x0C11
_GL_BLEND = 0x0BE2
_GL_ONE = 1
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_TRIANGLE_STRIP = 5
_GL_FLOAT = 0x1406
_GL_RGB10_A2 = 0x8059
_GL_UNPACK_ROW_LENGTH = 0x0CF2
_GL_UNPACK_ALIGNMENT = 0x0CF5

# The picture is re-rendered for a new video frame once the surface has grown
# or shrunk this much since it was last rendered; it is only scaled before
# that (as MpvPlayerView._paint_scaled does for the widget).
_RESCALE_LIMIT = 1.15

# BT.2408 reference white: SDR white in a PQ signal when the display does not
# say what it uses for SDR content.
DEFAULT_SDR_WHITE_NITS = 203.0

# A unit quad, drawn as a triangle strip and placed by the ``rect`` uniform.
_VERTEX = """
attribute highp vec2 vertex;
uniform highp vec4 rect;
uniform highp vec4 source;
varying highp vec2 uv;
void main() {
    uv = source.xy + vertex * source.zw;
    gl_Position = vec4(rect.xy + vertex * rect.zw, 0.0, 1.0);
}
"""

# Premultiplied sRGB overlay pixels. ``white`` > 0 converts them for a PQ
# surface: linear light at that fraction of 10,000 nits, BT.709 primaries in
# BT.2020, then the ST 2084 curve. 0 passes them through (an SDR surface).
_FRAGMENT = """
uniform sampler2D tex;
uniform highp float white;
uniform highp float opacity;
varying highp vec2 uv;
highp vec3 linear(highp vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}
highp vec3 pq(highp vec3 l) {
    highp vec3 p = pow(max(l, vec3(0.0)), vec3(0.1593017578125));
    return pow((vec3(0.8359375) + 18.8515625 * p) / (vec3(1.0) + 18.6875 * p), vec3(78.84375));
}
void main() {
    highp vec4 c = texture2D(tex, uv);
    if (white > 0.0 && c.a > 0.0) {
        highp mat3 bt709_to_bt2020 = mat3(
            0.627404, 0.069097, 0.016391,
            0.329283, 0.919540, 0.088013,
            0.043313, 0.011362, 0.895595);
        highp vec3 rgb = bt709_to_bt2020 * linear(c.rgb / c.a) * white;
        c = vec4(pq(rgb) * c.a, c.a);
    }
    gl_FragColor = c * opacity;
}
"""


def pq_surface_format() -> QSurfaceFormat:
    """10 bits a channel, tagged BT.2100 PQ (bt2020 primaries, st2084)."""
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setRedBufferSize(10)
    fmt.setGreenBufferSize(10)
    fmt.setBlueBufferSize(10)
    fmt.setAlphaBufferSize(2)
    fmt.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.Bt2100Pq))
    return fmt


def _fit(width: int, height: int, aspect: float) -> tuple[float, float, float, float]:
    """The rectangle a picture of ``aspect`` letterboxes to in ``width`` x ``height``."""
    if width / height > aspect:
        w, h = height * aspect, float(height)
    else:
        w, h = float(width), width / aspect
    return (width - w) / 2, (height - h) / 2, w, h


@dataclass(eq=False)
class _Layer:
    """One overlay widget's picture, kept current as the widget repaints."""

    widget: QWidget
    image: QImage | None = None
    dirty: QRegion = field(default_factory=QRegion)  # widget coordinates, still to render
    upload: QRect = field(default_factory=QRect)  # image pixels changed since the last upload
    # The widget's next repaint leaves its picture as it was: it moved, its
    # fade's opacity changed, or rendering it toggled its effect.
    expect_repaint: bool = False
    skipping: bool = False  # inside such a repaint
    opacity: float | None = None  # its opacity effect's, as last seen


class OverlayMirror(QObject):
    """Pictures of the widgets stacked over ``view``, kept as they repaint.

    The candidates are found afresh each time: the view's own children, then
    for the view and each ancestor the siblings after it (later siblings stack
    above earlier ones), bottom to top. A widget counts while it is visible
    and overlaps the view. Each is rendered into an image only where it
    repainted, as the window's own repaint reported it (Paint events); a move
    repaints a translucent widget in full, but leaves its picture as it was,
    so the repaint that follows a move is not rendered again; nor are the
    repaints of a fade, whose opacity is applied when compositing (QWidget.
    render draws nothing for a widget with a graphics effect: the effect is
    switched off around it).
    """

    def __init__(self, view: QWidget, changed):
        super().__init__(view)
        self._view = view
        self._changed = changed
        self._layers: dict[QWidget, _Layer] = {}
        self._watched: set[QWidget] = set()
        self._rendering = False
        self._failed = False

    @property
    def failed(self) -> bool:
        return self._failed

    def candidates(self) -> list[QWidget]:
        view = self._view
        widgets = [c for c in view.children() if isinstance(c, QWidget) and not c.isWindow()]
        widget = view
        while not widget.isWindow() and widget.parentWidget() is not None:
            parent = widget.parentWidget()
            siblings = [c for c in parent.children() if isinstance(c, QWidget) and not c.isWindow()]
            widgets += siblings[siblings.index(widget) + 1:]
            widget = parent
        return widgets

    def layers(self, dpr: float) -> list[tuple[_Layer, QPoint, float]]:
        """Visible overlays as (layer, position over the view, opacity),
        bottom to top, their pictures brought up to date."""
        view = self._view
        top = view.window()
        origin = view.mapTo(top, QPoint(0, 0))
        area = QRect(QPoint(0, 0), view.size())
        result = []
        seen = set()
        for widget in self.candidates():
            if widget not in self._watched:
                # Watched while hidden too: its Show must bring a compose.
                # Children added later are watched as they arrive (ChildAdded).
                self._watch(widget)
            if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
                continue
            pos = widget.mapTo(top, QPoint(0, 0)) - origin
            if not area.intersects(QRect(pos, widget.size())):
                continue
            layer = self._layers.get(widget)
            if layer is None:
                layer = self._layers[widget] = _Layer(widget)
            seen.add(widget)
            opacity = self._render(layer, dpr)
            result.append((layer, pos, opacity))
        for widget in [w for w in self._layers if w not in seen]:
            # Out of view or hidden: its picture is stale by the time it
            # returns (a Show repaints it in full anyway).
            del self._layers[widget]
        return result

    def forget(self) -> None:
        self._layers.clear()

    # -- rendering -------------------------------------------------------------

    def _render(self, layer: _Layer, dpr: float) -> float:
        widget = layer.widget
        size = QSize(round(widget.width() * dpr), round(widget.height() * dpr))
        if layer.image is None or layer.image.size() != size or layer.image.devicePixelRatio() != dpr:
            layer.image = QImage(size, QImage.Format_RGBA8888_Premultiplied)
            layer.image.setDevicePixelRatio(dpr)
            layer.image.fill(0)
            layer.dirty = QRegion(widget.rect())
        effect = widget.graphicsEffect()
        opacity = 1.0
        if isinstance(effect, QGraphicsOpacityEffect) and effect.isEnabled():
            opacity = effect.opacity()
        region = layer.dirty & QRegion(widget.rect())
        layer.dirty = QRegion()
        if region.isEmpty():
            return opacity
        painter = QPainter(layer.image)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        for rect in region:
            painter.fillRect(rect, Qt.transparent)
        painter.end()
        # QWidget.render draws nothing for a widget that carries a graphics
        # effect; the fade's opacity is applied when compositing instead.
        disabled = effect is not None and effect.isEnabled()
        self._rendering = True
        try:
            if disabled:
                effect.setEnabled(False)
            bounds = region.boundingRect()
            # DrawChildren only: DrawWindowBackground fills the widget with
            # its palette's Window colour first. Style sheet backgrounds draw
            # either way.
            widget.render(layer.image, bounds.topLeft(), region, QWidget.RenderFlag.DrawChildren)
        finally:
            if disabled:
                effect.setEnabled(True)
                layer.expect_repaint = True  # the toggle repaints the widget later
            self._rendering = False
        changed = QRect(
            int(bounds.x() * dpr), int(bounds.y() * dpr),
            int(bounds.width() * dpr) + 2, int(bounds.height() * dpr) + 2,
        ) & layer.image.rect()
        layer.upload = layer.upload.united(changed) if not layer.upload.isNull() else changed
        return opacity

    # -- change tracking -------------------------------------------------------

    def _watch(self, widget: QWidget) -> None:
        for w in [widget, *widget.findChildren(QWidget)]:
            if w not in self._watched and not w.isWindow():
                self._watched.add(w)
                w.installEventFilter(self)
                w.destroyed.connect(self._unwatch)

    def _unwatch(self, obj=None) -> None:
        self._watched.discard(obj)

    def _layer_of(self, widget: QWidget) -> _Layer | None:
        while widget is not None:
            layer = self._layers.get(widget)
            if layer is not None:
                return layer
            if widget.isWindow():
                return None
            widget = widget.parentWidget()
        return None

    def eventFilter(self, obj, event) -> bool:
        try:
            self._observe(obj, event)
        except Exception:  # noqa: BLE001 - see the module docstring
            self._failed = True
        return False

    def _observe(self, obj, event) -> None:
        kind = event.type()
        if kind == QEvent.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget) and obj in self._watched:
                self._watch(child)
            return
        if kind not in (QEvent.Paint, QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide, QEvent.ZOrderChange):
            return
        if not isinstance(obj, QWidget):
            return
        layer = self._layer_of(obj)
        if layer is None:
            # Not shown over the video yet: a Show or Move may bring it there.
            if kind != QEvent.Paint:
                self._changed()
            return
        root = layer.widget
        if kind == QEvent.Paint:
            if self._rendering:
                return
            if obj is root:
                effect = root.graphicsEffect()
                opacity = (effect.opacity() if isinstance(effect, QGraphicsOpacityEffect)
                           and effect.isEnabled() else None)
                if opacity != layer.opacity:
                    # A fade step: the composition applies the new opacity.
                    layer.opacity = opacity
                    if opacity is not None:
                        layer.expect_repaint = True
                if layer.expect_repaint:
                    # Its children repaint in the same pass, which ends
                    # before the next event is processed.
                    layer.expect_repaint = False
                    layer.skipping = True
                    QTimer.singleShot(0, lambda layer=layer: setattr(layer, "skipping", False))
                    self._changed()
                    return
                layer.dirty |= event.region()
            else:
                if layer.skipping:
                    return
                layer.dirty |= event.region().translated(obj.mapTo(root, QPoint(0, 0)))
        elif kind == QEvent.Move and obj is root:
            layer.expect_repaint = True  # a translucent widget repaints in full where it lands
        else:
            layer.dirty = QRegion(root.rect())
        self._changed()


class HdrPresenter(QOpenGLWindow):
    """A native child window over the player widget that mpv renders into.

    It follows the player's geometry, takes no input or focus, and composes
    mpv's last picture (rendered only for a new frame, or once the surface
    has settled at a new size) with the mirrored overlays. ``pq`` makes it a
    10-bit BT.2100 PQ surface; otherwise it is an ordinary SDR window (the
    RELAY_HDR_SURFACE=force test mode on an SDR display).
    """

    ready = Signal()
    failed = Signal(str)

    def __init__(self, view, parent: QWindow, *, pq: bool, sdr_white_nits: float | None = None):
        super().__init__(QOpenGLWindow.NoPartialUpdate, parent)
        self._view = view
        self.pq = pq
        self.sdr_white_nits = sdr_white_nits or DEFAULT_SDR_WHITE_NITS
        if pq:
            self.setFormat(pq_surface_format())
        self.setFlags(self.flags() | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.ctx = None  # mpv's render context while this surface owns it
        self._initialized = False
        self._broken = False
        self._fbo: QOpenGLFramebufferObject | None = None
        self._picture_due = False
        self._program: QOpenGLShaderProgram | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._textures: dict[_Layer, QOpenGLTexture] = {}
        self._mirror = OverlayMirror(view, self._overlays_changed)
        self._followed: list[QWidget] = []
        self._place_posted = False
        self.frames = 0  # composed frames, for measurements
        self.renders = 0  # mpv renders
        self.last_paint_ms = 0.0

    # -- placement -------------------------------------------------------------

    def follow_view(self) -> None:
        """Track the player widget's place in the window from now on."""
        view = self._view
        chain = []
        widget = view
        while widget is not None and not widget.isWindow():
            chain.append(widget)
            widget = widget.parentWidget()
        for widget in chain:
            if widget not in self._followed:
                widget.installEventFilter(self._tracker)
                self._followed.append(widget)
        self._place_now()

    @property
    def _tracker(self) -> QObject:
        tracker = getattr(self, "_tracker_obj", None)
        if tracker is None:
            tracker = self._tracker_obj = _GeometryTracker(self)
        return tracker

    def place(self) -> None:
        """Follow the player widget, once per turn of the event loop.

        A layout step moves and resizes the player and its ancestors one
        after another, each with its own event. On Wayland every geometry
        change of a subsurface makes Qt expose the parent window, which
        repaints and presents it in full at once; changing it for each event
        held the slides to 15-57 steps a second (110-186 on the widget). The
        layout settles within the turn, before the window's next frame.
        """
        if not self._place_posted:
            self._place_posted = True
            QTimer.singleShot(0, self._place_now)

    def _place_now(self) -> None:
        self._place_posted = False
        view = self._view
        geometry = QRect(view.mapTo(view.window(), QPoint(0, 0)), view.size())
        if geometry != self.geometry():
            self.setGeometry(geometry)
            self.update()

    def picture_due(self) -> None:
        """The current frame is to be rendered again (a resize began)."""
        self._picture_due = True

    # -- frames ------------------------------------------------------------------

    def frame_ready(self) -> None:
        """mpv has a frame (or a redraw) for this surface."""
        if self.ctx is None:
            return
        if self.isExposed():
            self.update()
            return
        # Hidden (another workspace): acknowledge the frame, as the widget
        # does, so mpv's VO does not time out and count it as dropped.
        self.makeCurrent()
        try:
            if self.ctx is not None and self.ctx.update():
                self.ctx.render(skip_rendering=True)
        finally:
            self.doneCurrent()

    def _overlays_changed(self) -> None:
        if self.isVisible():
            self.update()

    # -- GL ------------------------------------------------------------------------

    def initializeGL(self) -> None:
        try:
            self._init_gl()
            self._initialized = True
            self.ready.emit()
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._broken = True
            self.failed.emit(f"HDR surface: {error!r}")

    def _init_gl(self) -> None:
        program = QOpenGLShaderProgram(self)
        if not program.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERTEX):
            raise RuntimeError(program.log())
        if not program.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAGMENT):
            raise RuntimeError(program.log())
        program.bindAttributeLocation("vertex", 0)
        if not program.link():
            raise RuntimeError(program.log())
        vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        vbo.create()
        vbo.bind()
        vbo.allocate(struct.pack("8f", 0, 0, 1, 0, 0, 1, 1, 1), 32)
        vao = QOpenGLVertexArrayObject(self)
        if vao.create():
            vao.bind()
            program.enableAttributeArray(0)
            program.setAttributeBuffer(0, _GL_FLOAT, 0, 2)
            vao.release()
        else:
            vao = None
        vbo.release()
        self._program, self._vbo, self._vao = program, vbo, vao
        self.context().aboutToBeDestroyed.connect(self._release_gl)

    @property
    def initialized(self) -> bool:
        return self._initialized and not self._broken

    @property
    def broken(self) -> bool:
        return self._broken

    def _release_gl(self) -> None:
        """The context is going: free what lives in it, mpv's state first."""
        self.makeCurrent()
        try:
            ctx, self.ctx = self.ctx, None
            if ctx is not None:
                ctx.free()
                self._view._render_ctx_lost(ctx)
            self._drop_gl_objects()
        finally:
            self.doneCurrent()

    def _drop_gl_objects(self) -> None:
        self._fbo = None
        for texture in self._textures.values():
            texture.destroy()
        self._textures.clear()
        self._mirror.forget()

    def release_render_context(self) -> None:
        """Free mpv's render context here (it moves back to the widget)."""
        if self.ctx is None:
            return
        self.makeCurrent()
        try:
            self.ctx.free()
            self.ctx = None
            self._fbo = None
        finally:
            self.doneCurrent()

    def paintGL(self) -> None:
        started = time.perf_counter()
        try:
            if self._broken:
                self._clear()
                return
            self._paint()
            self.frames += 1
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._broken = True
            self.failed.emit(f"HDR surface: {error!r}")
        finally:
            self.last_paint_ms = (time.perf_counter() - started) * 1000

    def _clear(self) -> None:
        gl = self.context().functions()
        gl.glBindFramebuffer(_GL_FRAMEBUFFER, self.defaultFramebufferObject())
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)

    def _paint(self) -> None:
        dpr = self.devicePixelRatio()
        width, height = round(self.width() * dpr), round(self.height() * dpr)
        if width <= 0 or height <= 0:
            return
        gl = self.context().extraFunctions()
        target = self.defaultFramebufferObject()
        gl.glDisable(_GL_SCISSOR_TEST)
        gl.glBindFramebuffer(_GL_FRAMEBUFFER, target)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)
        if self.ctx is not None:
            self._paint_video(gl, target, width, height)
        gl.glBindFramebuffer(_GL_FRAMEBUFFER, target)
        layers = self._mirror.layers(dpr)
        if layers:
            self._paint_overlays(gl, layers, dpr, width, height)

    def _paint_video(self, gl, target: int, width: int, height: int) -> None:
        view = self._view
        aspect = view._picture_aspect()
        scaling = view._scaling and aspect is not None
        fbo = self._fbo
        if scaling:
            stale = fbo is None or not (
                1 / _RESCALE_LIMIT <= width / fbo.width() <= _RESCALE_LIMIT
                and 1 / _RESCALE_LIMIT <= height / fbo.height() <= _RESCALE_LIMIT)
        else:
            stale = fbo is None or fbo.width() != width or fbo.height() != height
        if stale:
            fmt = QOpenGLFramebufferObjectFormat()
            # 10 bits for mpv to dither to; it reads the depth of the
            # attachment (mpv_opengl_fbo.internal_format is ignored).
            fmt.setInternalTextureFormat(_GL_RGB10_A2)
            fbo = self._fbo = QOpenGLFramebufferObject(QSize(width, height), fmt)
        new_frame = self.ctx.update()
        if stale or new_frame or self._picture_due:
            # The window's own framebuffer is bottom-up, as GL draws; unlike
            # the widget's, nothing flips it afterwards.
            self.ctx.render(
                flip_y=False,
                opengl_fbo={"fbo": fbo.handle(), "w": fbo.width(), "h": fbo.height()},
            )
            self._picture_due = False
            self.renders += 1
        gl.glBindFramebuffer(_GL_READ_FRAMEBUFFER, fbo.handle())
        gl.glBindFramebuffer(_GL_DRAW_FRAMEBUFFER, target)
        if fbo.width() == width and fbo.height() == height:
            gl.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height, _GL_COLOR_BUFFER_BIT, _GL_LINEAR)
            return
        if aspect is None:
            gl.glBlitFramebuffer(0, 0, fbo.width(), fbo.height(), 0, 0, width, height,
                                 _GL_COLOR_BUFFER_BIT, _GL_LINEAR)
            return
        # Anchored on the letterboxed picture so it never stretches.
        src_x, src_y, src_w, _src_h = _fit(fbo.width(), fbo.height(), aspect)
        dst_x, dst_y, dst_w, _dst_h = _fit(width, height, aspect)
        scale = dst_w / src_w
        left, bottom = dst_x - src_x * scale, dst_y - src_y * scale
        gl.glBlitFramebuffer(
            0, 0, fbo.width(), fbo.height(),
            round(left), round(bottom),
            round(left + fbo.width() * scale), round(bottom + fbo.height() * scale),
            _GL_COLOR_BUFFER_BIT, _GL_LINEAR,
        )

    def _paint_overlays(self, gl, layers, dpr: float, width: int, height: int) -> None:
        program = self._program
        program.bind()
        if self._vao is not None:
            self._vao.bind()
        else:
            self._vbo.bind()
            program.enableAttributeArray(0)
            program.setAttributeBuffer(0, _GL_FLOAT, 0, 2)
        gl.glViewport(0, 0, width, height)
        if not self.context().isOpenGLES():
            gl.glPixelStorei(_GL_UNPACK_ROW_LENGTH, 0)  # mpv's uploads may leave it set
        gl.glPixelStorei(_GL_UNPACK_ALIGNMENT, 4)
        gl.glEnable(_GL_BLEND)
        gl.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
        program.setUniformValue1i("tex", 0)
        program.setUniformValue1f("white", self.sdr_white_nits / 10000.0 if self.pq else 0.0)
        # Overlay images are top-down: the quad's bottom samples their last row.
        program.setUniformValue("source", QVector4D(0.0, 1.0, 1.0, -1.0))
        live = set()
        for layer, pos, opacity in layers:
            image = layer.image
            if opacity <= 0.0 or image is None:
                continue
            texture = self._texture(layer)
            live.add(layer)
            texture.bind(0)
            x, y = round(pos.x() * dpr), round(pos.y() * dpr)
            w, h = image.width(), image.height()
            program.setUniformValue("rect", QVector4D(
                2.0 * x / width - 1.0, 1.0 - 2.0 * (y + h) / height, 2.0 * w / width, 2.0 * h / height))
            program.setUniformValue1f("opacity", float(opacity))
            gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        gl.glDisable(_GL_BLEND)
        if self._vao is not None:
            self._vao.release()
        program.release()
        for layer in [layer for layer in self._textures if layer not in live]:
            self._textures.pop(layer).destroy()

    def _texture(self, layer: _Layer) -> QOpenGLTexture:
        image = layer.image
        texture = self._textures.get(layer)
        if texture is not None and (texture.width() != image.width() or texture.height() != image.height()):
            self._textures.pop(layer).destroy()
            texture = None
        if texture is None:
            texture = QOpenGLTexture(QOpenGLTexture.Target2D)
            texture.setFormat(QOpenGLTexture.RGBA8_UNorm)
            texture.setSize(image.width(), image.height())
            texture.setMinificationFilter(QOpenGLTexture.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Linear)
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)
            texture.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8)
            self._textures[layer] = texture
            layer.upload = image.rect()
        rect = layer.upload
        if not rect.isNull() and not rect.isEmpty():
            part = image if rect == image.rect() else image.copy(rect)
            texture.setData(rect.x(), rect.y(), 0, rect.width(), rect.height(), 1,
                            QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, part.constBits())
        layer.upload = QRect()
        return texture


class _GeometryTracker(QObject):
    """Moves the presenter with the player widget (its moves and resizes, and
    those of every widget between it and the window)."""

    def __init__(self, presenter: HdrPresenter):
        super().__init__(presenter)
        self._presenter = presenter

    def eventFilter(self, obj, event) -> bool:
        try:
            if event.type() in (QEvent.Move, QEvent.Resize) and self._presenter.isVisible():
                self._presenter.place()
        except Exception:  # noqa: BLE001 - see the module docstring
            pass
        return False
