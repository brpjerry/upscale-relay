"""libmpv-backed player view.

The downlink is a streaming Matroska byte stream with original PTS
(docs/PROTOCOL.md §3.2), fed to mpv through a localhost TCP stream.
Audio/subtitles either arrive as negotiated embedded tracks (server-library
sessions) or from the original external file (local/legacy sessions). Both
paths align by absolute timestamps, including after a seek reloads a fresh
container at the epoch target.

The DLL is looked up in <repo>/mpv-dev (see _load_mpv).
"""

from __future__ import annotations

import asyncio
from collections import deque
from ctypes import c_void_p
import os
import socket
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QOpenGLContext, qRgb
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from . import hdr_output
from .hdr_view import HdrPresenter
from .options import DesktopOptions
from .playback_state import PlaybackTelemetry, match_track, track_descriptor
from .idle_inhibit import IdleInhibitor



def _thumbnail(shot: dict, columns: int = 16, rows: int = 9, reach: int = 6) -> QImage | None:
    """Reduce a raw mpv screenshot (bgr0) to a few short averages per row,
    enough to judge the picture's dominant colour."""
    width, height, stride, data = shot["w"], shot["h"], shot["stride"], shot["data"]
    if shot.get("format") != "bgr0" or width <= 0 or height <= 0:
        return None
    view = memoryview(data)
    image = QImage(columns, rows, QImage.Format_RGB32)
    for j in range(rows):
        line = int((j + 0.5) * height / rows) * stride
        for i in range(columns):
            centre = int((i + 0.5) * width / columns)
            span = view[line + max(0, centre - reach) * 4:line + min(width, centre + reach) * 4]
            count = len(span) // 4
            image.setPixel(i, j, qRgb(
                sum(span[2::4]) // count, sum(span[1::4]) // count, sum(span[0::4]) // count))
    return image


def _load_mpv():
    mpv_dir = Path(__file__).resolve().parents[1] / "mpv-dev"
    if os.name == "nt" and mpv_dir.is_dir():
        os.add_dll_directory(str(mpv_dir))
        os.environ["PATH"] = str(mpv_dir) + ";" + os.environ.get("PATH", "")
    import mpv  # noqa: PLC0415

    return mpv


mpv = _load_mpv()

# Start with a half-second mpv reserve once the first cluster is decodable.
# Cache pause remains enabled for recovery; this only reduces its initial wait.
CACHE_PAUSE_WAIT_S = 0.5

# Properties the periodic stats read, kept current by mpv's event thread.
_OBSERVED_PROPERTIES = (
    "time-pos", "duration", "avsync", "frame-drop-count",
    "demuxer-cache-duration", "paused-for-cache", "seeking", "path",
    "volume", "mute", "audio-delay", "sub-delay", "aid", "sid",
    "hwdec-current", "video-params",
)
# Picture geometry for drawing the last rendered picture scaled while the
# view is being resized. Anything but plain letterboxing renders at each size.
_GEOMETRY_PROPERTIES = (
    "dwidth", "dheight", "panscan", "video-zoom", "video-pan-x", "video-pan-y",
    "video-align-x", "video-align-y", "video-unscaled", "keepaspect", "video-rotate",
)
# The picture is re-rendered for a new video frame once the view has grown or
# shrunk this much since it was last rendered; it is only scaled before that.
_RESCALE_LIMIT = 1.15
# The view counts as resizing until this long after its last resize; then the
# picture is rendered sharp at the size it settled at.
_RESIZE_SETTLE_MS = 100
_GL_FRAMEBUFFER = 0x8D40
_GL_READ_FRAMEBUFFER = 0x8CA8
_GL_DRAW_FRAMEBUFFER = 0x8CA9
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_LINEAR = 0x2601
_GL_SCISSOR_TEST = 0x0C11
# How long the HDR surface may take to appear before a load falls back to
# the widget, and how often the display's HDR state is read during an HDR
# video (a Wayland query is four roundtrips; DXGI a fresh factory).
_HDR_SURFACE_TIMEOUT_S = 3.0
_HDR_RECHECK_TICKS = 6  # stats ticks (0.5 s)
# How long the compositor may take to switch a fullscreen screen to HDR for
# the HDR surface (Hyprland: ~1.2 s, 2026-11-02) before the video goes back
# to the widget, tone-mapped.
_AUTO_HDR_TIMEOUT_S = 5.0


def _fit(width: int, height: int, aspect: float) -> tuple[float, float, float, float]:
    """The rectangle a picture of ``aspect`` letterboxes to in ``width`` x ``height``."""
    if width / height > aspect:
        w, h = height * aspect, float(height)
    else:
        w, h = float(width), width / aspect
    return (width - w) / 2, (height - h) / 2, w, h


def _render_hwdec_mode(no_hwdec: bool, platform: str | None = None) -> str:
    """Return the hardware decode mode safe for the embedded render path.

    Intel's zero-copy VA-API path can let Qt paint a surface while iHD is
    retiring it during a live-stream transition.  The captured crash was
    paintGL -> mpv_render_context_render -> vaSyncSurface -> iHD.  Copy-back
    keeps hardware decode but gives the render API an ordinary software frame.
    Other platforms retain mpv's safe automatic selection.
    """
    if no_hwdec:
        return "no"
    platform = platform or sys.platform
    return "auto-copy-safe" if platform.startswith("linux") else "auto-safe"


def _mpv_config_dirs(environ=None, platform: str | None = None) -> list[Path]:
    """mpv's user configuration folders, highest priority first.

    libmpv runs here without its configuration (mpv.conf is not read), and
    without it mpv no longer resolves these folders itself, so they are found
    the way mpv finds them.
    """
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    if environ.get("MPV_HOME"):
        return [Path(environ["MPV_HOME"])]
    if platform.startswith("win"):
        return [Path(environ["APPDATA"]) / "mpv"] if environ.get("APPDATA") else []
    config = Path(environ["XDG_CONFIG_HOME"]) if environ.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return [config / "mpv", Path.home() / ".mpv"]


def user_input_conf(environ=None, platform: str | None = None) -> Path | None:
    """The input.conf mpv itself would load for this user, if there is one."""
    return next((folder / "input.conf" for folder in _mpv_config_dirs(environ, platform)
                 if (folder / "input.conf").is_file()), None)


def _user_scripts(environ=None, platform: str | None = None) -> list[Path]:
    """The scripts in mpv's scripts folder (--mpv-scripts)."""
    for folder in _mpv_config_dirs(environ, platform):
        if (folder / "scripts").is_dir():
            return sorted(p for p in (folder / "scripts").iterdir() if not p.name.startswith("."))
    return []


def _native_display_params(app=None) -> dict[str, c_void_p]:
    """Return Qt's display handle in the form expected by libmpv.

    The render API cannot discover Qt's Wayland/X11 connection itself.  If it
    is omitted, GPU interop setup fails (notably VA-API on Linux) and mpv
    silently falls back to software decoding.
    """
    app = app or QGuiApplication.instance()
    if app is None:
        return {}
    platform = app.platformName().lower()
    if platform.startswith("wayland"):
        param_name = "wl_display"
    elif platform == "xcb":
        param_name = "x11_display"
    else:
        return {}
    try:
        native = app.nativeInterface()
        display = native.display()
        address = int(display)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return {}
    if not address:
        return {}
    return {param_name: c_void_p(address)}


class _LoopbackStream:
    """Thread-safe byte pipe served to mpv over a native localhost socket.

    python-mpv's custom-stream adapter copies callback data into libmpv one
    byte at a time in Python. That capped lossless HEVC near 200 Mbps even
    with ten seconds already queued here. A dedicated sender thread lets
    libmpv/FFmpeg receive the same Matroska bytes through its native TCP
    protocol without involving qasync or a Python callback on every read.
    """

    def __init__(self):
        self._chunks: deque[bytes] = deque()
        self._queued_bytes = 0
        self._total_fed_bytes = 0
        self._total_read_bytes = 0
        self._cond = threading.Condition()
        self._finished = False
        self._aborted = False
        self._connection: socket.socket | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.25)
        host, port = self._listener.getsockname()
        self.uri = f"tcp://{host}:{port}"
        self._thread = threading.Thread(
            target=self._serve, name=f"mpv-loopback-{port}", daemon=True,
        )
        self._thread.start()

    def feed(self, data: bytes) -> None:
        with self._cond:
            if self._finished or self._aborted:
                return
            self._chunks.append(data)
            self._queued_bytes += len(data)
            self._total_fed_bytes += len(data)
            self._cond.notify_all()

    def finish(self) -> None:
        """Send all queued bytes, then signal EOF to mpv."""
        with self._cond:
            self._finished = True
            self._cond.notify_all()

    def abort(self) -> None:
        """Stop immediately, discarding bytes from a superseded stream."""
        with self._cond:
            self._aborted = True
            self._chunks.clear()
            self._queued_bytes = 0
            connection = self._connection
            self._cond.notify_all()
        for sock in (connection, self._listener):
            if sock is not None:
                try:
                    # close() in another thread does not interrupt a blocking
                    # sendall() on Linux. Shutdown wakes the native sender even
                    # when mpv has stopped reading without closing its socket.
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)

    # The old byte-pipe API used close() everywhere. Keep close as the
    # immediate lifecycle operation; end-of-content explicitly uses finish().
    close = abort

    def _next_chunk(self) -> bytes:
        with self._cond:
            while not self._chunks and not self._finished and not self._aborted:
                self._cond.wait(timeout=1.0)
            if self._chunks:
                data = self._chunks.popleft()
                self._queued_bytes -= len(data)
                return data
            return b""

    def _serve(self) -> None:
        connection = None
        try:
            while not self._aborted:
                try:
                    connection, _ = self._listener.accept()
                    break
                except TimeoutError:
                    continue
                except OSError:
                    return
            if connection is None:
                return
            try:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            with self._cond:
                # abort() may have run between accept() and publication. In
                # that case its socket snapshot could not contain this peer.
                if self._aborted:
                    return
                self._connection = connection
            while not self._aborted:
                data = self._next_chunk()
                if not data:
                    break
                connection.sendall(data)
                with self._cond:
                    self._total_read_bytes += len(data)
            if not self._aborted:
                try:
                    connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        except OSError:
            # mpv closes the socket during stop/reload; that is normal.
            pass
        finally:
            with self._cond:
                self._connection = None
            for sock in (connection, self._listener):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def stats(self) -> dict:
        with self._cond:
            return {
                "chunks": len(self._chunks),
                "queued_bytes": self._queued_bytes,
                "total_fed_bytes": self._total_fed_bytes,
                "total_read_bytes": self._total_read_bytes,
            }


def _get_proc_address(_ctx, name: bytes) -> int:
    glctx = QOpenGLContext.currentContext()
    if glctx is None:
        return 0
    return int(glctx.getProcAddress(name))


# Qt named keys -> mpv key names (printable characters come from event.text()).
_QT_MPV_KEYS = {
    Qt.Key_Space: "SPACE", Qt.Key_Return: "ENTER", Qt.Key_Enter: "KP_ENTER",
    Qt.Key_Tab: "TAB", Qt.Key_Backspace: "BS", Qt.Key_Delete: "DEL",
    Qt.Key_Insert: "INS", Qt.Key_Home: "HOME", Qt.Key_End: "END",
    Qt.Key_PageUp: "PGUP", Qt.Key_PageDown: "PGDWN",
    Qt.Key_Left: "LEFT", Qt.Key_Right: "RIGHT",
    Qt.Key_Up: "UP", Qt.Key_Down: "DOWN",
    Qt.Key_NumberSign: "SHARP",
}
_QT_MPV_KEYS.update({getattr(Qt, f"Key_F{i}"): f"F{i}" for i in range(1, 13)})


def _mpv_key_name(event) -> str | None:
    """Translate a QKeyEvent to an mpv key name (input.conf syntax), or None."""
    key = event.key()
    mods = event.modifiers()
    base = _QT_MPV_KEYS.get(key)
    shift_in_char = False  # shift already encoded in a printable character?
    if base is None:
        text = event.text()
        if len(text) == 1 and text.isprintable():
            base, shift_in_char = text, True
            if mods & Qt.ShiftModifier and base.isalpha():
                base = base.upper()  # synthetic events may carry lowercase text
        elif Qt.Key_A <= key <= Qt.Key_Z:  # ctrl/alt combos: text() is empty
            base = chr(key).lower()
        else:
            return None
    prefix = ""
    if mods & Qt.ControlModifier:
        prefix += "ctrl+"
    if mods & Qt.AltModifier:
        prefix += "alt+"
    if mods & Qt.MetaModifier:
        prefix += "meta+"
    if mods & Qt.ShiftModifier and not shift_in_char:
        prefix += "shift+"
    return prefix + base


class MpvPlayerView(QOpenGLWidget):
    telemetry_changed = Signal(object)
    log_message = Signal(str, str, str)
    _terminal = Signal(int, bool, str)
    stats_changed = Signal(str)
    position_changed = Signal(float)  # seconds
    track_list_changed = Signal(list, object)  # [(sid, title)] subs, selected sid
    audio_track_list_changed = Signal(list, object)  # [(aid, title)] audio, selected aid
    volume_changed = Signal(int, bool)  # volume percent, muted
    rebuffering = Signal(bool)
    pause_requested = Signal()  # keyboard and toolbar share application intent
    paused_changed = Signal(bool)  # an input.conf binding paused/resumed mpv itself
    seek_requested = Signal(float)  # relative seconds (arrow keys)
    chapter_step_requested = Signal(int)  # +1 next / -1 previous (PgUp/PgDn)
    finished = Signal()
    failed = Signal(str)
    fullscreen_toggled = Signal()  # F key / double-click; the window fullscreens
    mouse_moved = Signal(int, int)  # cursor x, y in the view; drives fullscreen control reveal
    frame_sampled = Signal(object)  # QImage thumbnail of the picture, on request
    # The video moved to (True) or from the HDR surface at a load.
    hdr_output_changed = Signal(bool)
    # During an HDR video the display's HDR state (or the setting) changed so
    # that the surface it plays on no longer fits: reload to switch.
    hdr_surface_stale = Signal()
    _frame_ready = Signal()  # mpv render thread -> queued repaint on GUI thread
    _playback_restarted = Signal(int)  # libmpv event thread -> GUI thread
    _external_media_ready = Signal(int)  # attach worker -> GUI thread
    _idle_state_changed = Signal()  # native property observer -> GUI thread
    _pause_observed = Signal()  # native property observer -> GUI thread

    # Bare keys the app reserves: arrows are NOT forwarded (mpv can't seek
    # the live stream — they emit seek_requested for a relay-protocol seek),
    # PgUp/PgDn are relay chapter steps for the same reason (mpv's builtin
    # chapter seek would act on the chapter-less live stream), F is the Qt
    # window's fullscreen (mpv has no window on the render-API path), Esc
    # propagates up to exit fullscreen. Everything else is translated and
    # forwarded so user input.conf bindings work.
    _SEEK_KEYS = {
        Qt.Key_Left: -5.0, Qt.Key_Right: 5.0,
        Qt.Key_Down: -60.0, Qt.Key_Up: 60.0,
    }
    _CHAPTER_KEYS = {Qt.Key_PageUp: 1, Qt.Key_PageDown: -1}
    # mpv's builtin quit bindings would shut down the embedded core and take
    # the whole player view with it — never forwarded.
    _BLOCKED_KEYS = {"q", "Q", "POWER", "CLOSE_WIN"}

    # mpv owns cache sizing/pause. Waiting for 1.5 seconds of packets in Python
    # added that interval to every start and could race loadfile's pause state.
    STARTUP_PACKETS = 1

    def __init__(self, parent=None, options: DesktopOptions | None = None):
        super().__init__(parent)
        self.options = options or DesktopOptions()
        # Read by resizeEvent/paintGL, so set before anything can resize the view.
        self._ctx = None  # MpvRenderContext, created in initializeGL
        self._resizing = False
        # While the view is being resized (slides, fullscreen, splitter drags)
        # the last rendered picture is drawn scaled instead; see _paint_scaled.
        self._scaling = False
        self._scaling_broken = False
        self._picture_fbo: QOpenGLFramebufferObject | None = None
        self._picture_due = False
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(_RESIZE_SETTLE_MS)
        self._settle_timer.timeout.connect(self._resizes_settled)
        # mpv renders through the libmpv render API into this QOpenGLWidget's
        # framebuffer (initializeGL/paintGL below) — no native child window.
        # The old wid embed needed a real X11 window: on Wayland it forced
        # XWayland (broken HiDPI) or popped a separate mpv window, and the
        # native subsurface drew sibling widgets duplicated/shifted.
        self.setMinimumSize(480, 270)
        self.setFocusPolicy(Qt.StrongFocus)  # receive keys for mpv forwarding
        self.setMouseTracking(True)  # deliver mouse-move without a pressed button
        self._sample_pending = False
        self._sampling_broken = False
        self._screen_watched = False
        self._report_display = False
        self._display_rate = 0.0
        self._get_proc = None  # ctypes callback — must outlive the render ctx
        self._frame_ready.connect(self._on_frame_ready, Qt.QueuedConnection)
        # HDR output (hdr_view.py): for an HDR video on an HDR display the
        # render context moves, between loads, to a native PQ surface over
        # this widget. Everything else (SDR videos, SDR displays) renders
        # here exactly as before.
        self._presenter: HdrPresenter | None = None
        self._on_presenter = False  # the render context lives on the presenter
        self._presenter_broken = False
        self._source_hdr = False  # the stream (or file) being played is PQ/HLG
        self._hdr_enabled = True  # the "Show HDR videos in HDR" setting
        self._display_hdr = hdr_output.SDR  # as last read
        self._hdr_stale_reported = False
        # In fullscreen on an SDR screen, Hyprland switches the screen to HDR
        # itself for an HDR surface (render:cm_auto_hdr). _auto_hdr: the
        # screen's HDR is that switch (until it reads SDR again);
        # _auto_hdr_deadline: the switch has yet to come; _auto_hdr_refused:
        # it did not, so do not ask again.
        self._auto_hdr = False
        self._auto_hdr_deadline: float | None = None
        self._auto_hdr_refused = False
        # mpv's output colour options as last set (its defaults at start).
        self._output_target = {"target-trc": "auto", "target-prim": "auto", "target-peak": "auto"}

        if self.options.headless:
            extra = {
                "vo": "null",
                "ao": "null",
                "osc": "no",
                # Headless runs exercise the same absolute-PTS epoch protocol
                # as the rendered UI. Without this, every post-seek Matroska
                # stream is rebased to zero and its position/audio clock no
                # longer matches the relay target.
                "rebase_start_time": "no",
                "cache": "yes",
                "cache_pause": "yes",
                "cache_pause_wait": CACHE_PAUSE_WAIT_S,
                "demuxer_readahead_secs": "15",
                "demuxer_max_bytes": "768MiB",
                "demuxer_max_back_bytes": "0",
            }
        else:
            # Deep readahead: mpv's default is ~1s beyond playback, so any
            # pipeline hiccup >1s stuttered even with plenty buffered client-
            # side. Let mpv itself hold 15s (sized for ~200 Mbps lossless).
            extra = {
                "vo": "libmpv",  # render API drives output; no mpv-owned window
                # No mpv.conf: libmpv loads no configuration by default, and
                # the mpv options this player offers are its own settings
                # (AppSettings.mpv_defaults, set at runtime). Only the key
                # bindings are the user's, from input.conf (below). User
                # scripts stay off: LuaJIT scripts hit the same stream-reload
                # instability as the stock OSC (--mpv-scripts opts in).
                "load_scripts": "no",
                # Screenshots (s, S) as lossless PNG in the Pictures folder;
                # mpv's own defaults are JPEG in the working directory.
                "screenshot_format": "png",
                # Standard mpv keys (m mute, 9/0 volume, i stats, s
                # screenshot…) — off by default in libmpv; keys reach mpv
                # via keyPressEvent forwarding below.
                "input_default_bindings": "yes",
                "cache": "yes",
                "cache_pause": "yes",
                "cache_pause_wait": CACHE_PAUSE_WAIT_S,
                "demuxer_readahead_secs": "15",
                "demuxer_max_bytes": "768MiB",
                "demuxer_max_back_bytes": "0",
                # Keep original timestamps: post-seek streams begin at the
                # seek target's absolute PTS. With rebasing (mpv default) the
                # timeline restarts at 0, which desyncs the external audio
                # and made `start=<target>` seek beyond the data (freeze).
                "rebase_start_time": "no",
                # Hand each frame over at its display time rather than 50 ms
                # (about a period at 24 fps) before it. render() blocks until
                # that time on the GUI thread, so the default spent ~40 of
                # every 42 ms there: resizes and slides ran at 12-14 fps.
                # Frames still land on time (3 ms median after target, 99.6-
                # 100% on the 5-vsync cadence of 24 fps at 120 Hz, against
                # 83-85% with the default). Display sync ignores this option.
                "video_timing_offset": 0,
            }
            pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
            if pictures:
                extra["screenshot_dir"] = pictures
            # Hardware decode for the HEVC tiers; FFV1 has no hw decoder and
            # mpv falls back silently. Linux uses copy-back: an actual core
            # captured Intel iHD crashing in vaSyncSurface from paintGL when a
            # user's mpv.conf (read back then) forced zero-copy hwdec=vaapi.
            # --no-hwdec disables.
            extra["hwdec"] = _render_hwdec_mode(self.options.no_hwdec)
            # OSC is OFF by default: it's a LuaJIT script that re-initializes
            # on every stream reload (seek), and that path intermittently
            # crashed mpv's event thread (native AV, exception 0xe24c4a02 =
            # LuaJIT) on a cold/slow pipeline. Our Qt controls cover the same
            # ground. --mpv-osc re-enables it for anyone who wants the
            # native overlay and rarely seeks. NB: on the render-API path mpv
            # has no window, so the OSC is display-only (no mouse input).
            if self.options.mpv_osc:
                extra["osc"] = "yes"
                extra.update({
                    "input_default_bindings": "yes",
                    "input_vo_keyboard": "yes",
                    "input_cursor": "yes",
                })
        # Isolated runs (tests, settings scopes) name their own file.
        input_conf = self.options.input_conf_path or user_input_conf()
        if input_conf is not None and Path(input_conf).is_file():
            extra["input_conf"] = str(input_conf)
        self.mpv = mpv.MPV(
            log_handler=lambda level, prefix, message: self.log_message.emit(level, prefix, message),
            loglevel="warn",
            keep_open="no",
            idle="yes",
            # The player keeps its own history. With mpv's resume, opening a
            # local file read standalone mpv's watch-later entry for it: the
            # entry was deleted and its volume, panscan, tracks and delays
            # applied here (only the position was overridden, by start=).
            resume_playback="no",
            **extra,
        )
        self._default_sub_fonts_dir = getattr(self.mpv, "sub_fonts_dir", "")
        if self.options.mpv_scripts:
            # Without its configuration mpv finds no scripts folder: load the
            # scripts by path. Their script-opts files are not read.
            for script in _user_scripts():
                try:
                    self.mpv.command("load-script", str(script))
                except Exception as err:  # noqa: BLE001 - one bad script
                    self.log_message.emit("error", "relay", f"mpv script {script.name}: {err!r}")
        # Player messages stay legible over bright video. OSD styling is
        # separate from the source's subtitle styling.
        self.mpv.osd_shadow_color = "#FF000000"
        self.mpv.osd_shadow_offset = 2
        self._buffer: _LoopbackStream | None = None
        self._task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._source_path: str | None = None
        self._downlink_codec: str | None = None
        self._source_has_audio = True
        self._local_playback = False
        self._local_load_ready: asyncio.Future | None = None
        self._pending_local_seek: float | None = None
        self._last_audio_output: tuple[int, bool] | None = None
        self._fps = 30.0
        self._fed = 0
        self._pending_start: float | None = None  # seek target for next reload
        self._epoch_base = 0  # frames fed before the current epoch's stream
        self._tracks_reported = False
        self._cached_tracks = []
        self._cached_audio_delay = 0.0
        self._cached_sub_delay = 0.0
        self._remembered_tracks = {}
        self._defaults = {}
        self._relay_eos_epoch = None
        self._transport_failed = False
        self._entry_generations = {}
        self._chosen_subtitle_id: int | None = None
        self._subtitle_choice_made = False
        self._chosen_audio_id: int | None = None
        self._audio_choice_made = False
        self._reloading = False
        self._load_generation = 0
        self._reload_settle_until = 0.0
        self._accept_playback_restart = False
        self._restart_seen = False
        self._external_attach_started = False
        self._external_ready = False
        self._prebuffer_ready = False
        self._epoch_released = False
        self._caller_paused = False
        self.client = None
        self._idle_inhibitor = IdleInhibitor(enabled=not self.options.headless)
        self._mpv_idle = True
        self._mpv_paused = True
        self._idle_state_changed.connect(self._sync_idle_inhibition, Qt.QueuedConnection)
        self._pause_observed.connect(self._adopt_native_pause, Qt.QueuedConnection)

        @self.mpv.property_observer("idle-active")
        def on_idle(_name, value):
            self._mpv_idle = value is not False
            self._idle_state_changed.emit()

        @self.mpv.property_observer("pause")
        def on_pause(_name, value):
            self._mpv_paused = value is not False
            self._idle_state_changed.emit()
            self._pause_observed.emit()

        # A synchronous read waits for mpv's core, which can itself be waiting
        # for this thread to render: under display sync two reads in one stats
        # tick stalled ~210 ms. The GUI thread reads these from the cache.
        self._observed: dict[str, object] = {}
        for name in _OBSERVED_PROPERTIES + _GEOMETRY_PROPERTIES:
            self.mpv.observe_property(name, self._on_observed)

        self._playback_restarted.connect(self._on_playback_restarted)
        self._external_media_ready.connect(self._on_external_media_ready)

        @self.mpv.event_callback("playback-restart")
        def on_restart(_event):
            # libmpv invokes callbacks on its event thread. Hop back to Qt's
            # thread before changing properties or starting the attach worker.
            if self._accept_playback_restart:
                self._playback_restarted.emit(self._load_generation)

        self._terminal.connect(self._on_terminal)

        @self.mpv.event_callback("start-file", "end-file")
        def on_end(event):
            entry = event.data.playlist_entry_id
            if event.event_id.value == mpv.MpvEventID.START_FILE:
                self._entry_generations[entry] = self._load_generation
                return
            generation = self._entry_generations.pop(entry, -1)
            if self._reloading or generation != self._load_generation:
                return
            reason = str(getattr(event.data, "reason", ""))
            if reason in ("MpvEventEndFile.EOF", "eof", "0"):
                natural = self._local_playback or (
                    self._relay_eos_epoch is not None
                    and (self.client is None or self._relay_eos_epoch == self.client.epoch)
                    and not self._transport_failed)
                self._terminal.emit(generation, natural, "Stream ended without confirmed relay EOS")
            elif reason in ("MpvEventEndFile.ERROR", "error", "4"):
                self._terminal.emit(generation, False, f"mpv playback error {event.data.error}")

    def _on_observed(self, name, value) -> None:
        # libmpv event thread; a dict store is atomic under the GIL.
        self._observed[name] = value

    def _observation(self, name: str, default=None):
        value = self._observed.get(name)
        return default if value is None else value

    def _on_terminal(self, generation, natural, message):
        if generation != self._load_generation or self._reloading:
            return
        if natural:
            self.finished.emit()
        else:
            self.failed.emit(message)

    def _sync_idle_inhibition(self) -> None:
        # Observe mpv's actual pause state, including input.conf bindings. Do
        # not read native properties here: stop/reload can still be tearing down.
        self._idle_inhibitor.set_active(
            (self._task is not None or self._local_playback)
            and not self._mpv_idle and not self._mpv_paused
        )

    def _adopt_native_pause(self) -> None:
        # An input.conf binding (cycle pause, frame-step…) pauses mpv without
        # the application seeing the command. Make it the caller's intent, or
        # the next epoch release, restart or fallback undoes it. The player's
        # own pauses never get here: the load-time hold and the pause value
        # mpv restores when a file stops arrive while the epoch is held or
        # retired, and our own writes set _caller_paused first. Reading the
        # latest observation skips a value superseded before this ran.
        paused = self._mpv_paused
        if (not self._epoch_released or self._reloading
                or paused == self._caller_paused):
            return
        self._caller_paused = paused
        self.paused_changed.emit(paused)

    # -- rendering (libmpv render API) ------------------------------------------

    def initializeGL(self) -> None:
        if self.options.headless or self._ctx is not None:
            return
        self._ctx = self._create_render_ctx()
        # The GL context is destroyed before the widget on teardown — free the
        # render context first, while the GL context is still alive.
        self.context().aboutToBeDestroyed.connect(self._free_render_ctx)

    def _create_render_ctx(self):
        """mpv's render context, in the GL context current now (this
        widget's, or the HDR surface's)."""
        if self._get_proc is None:
            self._get_proc = mpv.MpvGlGetProcAddressFn(_get_proc_address)
        render_params = {
            "opengl_init_params": {"get_proc_address": self._get_proc},
            **_native_display_params(),
        }
        ctx = mpv.MpvRenderContext(
            self.mpv, "opengl",
            **render_params,
        )
        ctx.update_cb = self._frame_ready.emit
        return ctx

    def showEvent(self, event) -> None:
        super().showEvent(event)
        handle = self.window().windowHandle()
        if handle is not None and not self._screen_watched:
            self._screen_watched = True
            handle.screenChanged.connect(lambda _screen: self._report_display_rate())
            handle.screenChanged.connect(lambda _screen: self._check_hdr_surface())
        self._report_display_rate()

    def set_display_rate_reporting(self, enabled: bool) -> None:
        """Opt in to (or out of) telling mpv the display's refresh rate."""
        self._report_display = bool(enabled)
        self._report_display_rate()

    def _report_display_rate(self) -> None:
        """Tell mpv the refresh rate of the screen this window is on.

        Through the render API mpv cannot see the display, so its display-sync
        modes (`video-sync=display-*`) silently fall back to audio timing.
        Given the rate, such a mode renders once per refresh instead of once
        per video frame: about 3 W more on the laptop on battery. Opt-in for
        that reason; with it off the rate is left unknown (0), and
        `video-sync=audio` is unaffected either way.

        Reported only while the window is presenting. Display sync takes each
        render call as one refresh, and a hidden window has none: its frames
        are acknowledged at once (_on_frame_ready), so mpv ran the video as
        fast as it could decode (~3x realtime from the server, 4,000
        acknowledgements a second), drained the relay buffer, and on return
        held the picture at ~2 fps while audio caught up a minute later.
        """
        screen = self.screen()
        rate = (screen.refreshRate() if screen is not None and self._report_display and self._presenting()
                else 0.0)
        if self.options.headless or rate < 0 or rate == self._display_rate:
            return
        try:
            self.mpv["display-fps-override"] = float(rate)
            self._display_rate = rate
        except Exception:
            pass

    def request_frame_sample(self) -> None:
        """Ask for a thumbnail of the current picture (``frame_sampled``).

        The picture comes from mpv's own asynchronous raw screenshot, taken on
        mpv's threads from the decoded frame. Reading the widget's framebuffer
        back instead stalled the GUI thread ~150 ms per read here, and the
        render path is not a place to experiment (see CLAUDE.md hard rules).
        """
        if (self._sample_pending or self._sampling_broken or self.options.headless
                or self._reloading or not self._epoch_released):
            return
        self._sample_pending = True
        try:
            self.mpv.command_async("screenshot-raw", "video", callback=self._on_raw_screenshot)
        except Exception:
            self._sample_pending = False
            self._sampling_broken = True

    def _on_raw_screenshot(self, error, result) -> None:
        # libmpv event thread. Only bytes are touched here; the tiny image
        # crosses to the GUI thread through the (queued) signal.
        self._sample_pending = False
        if error or not result:
            return
        try:
            image = _thumbnail(result)
        except Exception:
            self._sampling_broken = True
            return
        if image is not None:
            self.frame_sampled.emit(image)

    def _free_render_ctx(self) -> None:
        if self._ctx is not None and not self._on_presenter:
            self.makeCurrent()
            self._picture_fbo = None  # its GL objects go with this context
            self._ctx.free()
            self._ctx = None
            self.doneCurrent()

    def resizeEvent(self, event) -> None:
        # QOpenGLWidget re-allocates its framebuffer here and paints into it at
        # once; a slide step resized it two or three times per frame, and each
        # was an mpv render at the new size (~4.5 ms at fullscreen sizes, of a
        # 8.3 ms frame; under display sync mpv also counts each as a vsync).
        # Skip those and request one repaint instead, which renders the new
        # framebuffer before the window is next composited. Qt does not always
        # repaint the widget after a resize on its own: without the request,
        # frames were composited from the unrendered framebuffer.
        if not self._scaling and self._picture_aspect() is not None:
            self._scaling = True
            self._picture_due = True  # the current frame, into our framebuffer
            if self._on_presenter:
                self._presenter.picture_due()
        if self._scaling:
            self._settle_timer.start()
        self._resizing = True
        try:
            super().resizeEvent(event)
        finally:
            self._resizing = False
        self.update()

    def _resizes_settled(self) -> None:
        self._scaling = False
        self.update()  # one sharp render at the size it settled at
        if self._on_presenter:
            self._presenter.update()

    def _picture_aspect(self) -> float | None:
        """The picture's display aspect, when mpv simply letterboxes it."""
        if self._ctx is None or self._scaling_broken:
            return None
        observed = self._observed
        plain = (
            not observed.get("panscan") and not observed.get("video-zoom")
            and not observed.get("video-pan-x") and not observed.get("video-pan-y")
            and not observed.get("video-align-x") and not observed.get("video-align-y")
            and not observed.get("video-unscaled") and not observed.get("video-rotate")
            and observed.get("keepaspect") is not False
        )
        width, height = observed.get("dwidth"), observed.get("dheight")
        if not plain or not width or not height:
            return None
        return width / height

    def paintGL(self) -> None:
        if self._on_presenter:
            # The HDR surface covers this widget and mpv renders there. Black
            # is what shows at its edges while it catches up with a resize.
            gl = QOpenGLContext.currentContext().functions()
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            gl.glClear(_GL_COLOR_BUFFER_BIT)
            return
        if self._ctx is None or self._resizing:
            return
        # QOpenGLWidget's backing FBO is in physical pixels (HiDPI-scaled).
        dpr = self.devicePixelRatioF()
        width, height = round(self.width() * dpr), round(self.height() * dpr)
        if self._scaling:
            try:
                if self._paint_scaled(width, height):
                    return
            except Exception as error:  # noqa: BLE001 - see below
                # An exception escaping a Qt virtual is a native crash, not a
                # traceback. Render at every size from now on instead.
                self._scaling_broken = True
                self._scaling = False
                self.log_message.emit("error", "relay", f"scaled resize disabled: {error!r}")
        else:
            self._picture_fbo = None  # free it with this context current
        self._ctx.render(
            flip_y=True,
            opengl_fbo={"fbo": self.defaultFramebufferObject(), "w": width, "h": height},
        )

    def _paint_scaled(self, width: int, height: int) -> bool:
        """Draw the last rendered picture scaled to this size; re-render it
        only for a new video frame, at this size once it has grown or shrunk
        past _RESCALE_LIMIT.

        Rendering at every size of a slide cost up to ~5 ms of the 8.3 ms
        frame (mpv re-creates its targets at each new size), which held
        fullscreen transitions to ~90 fps and 4K ones to ~65. A scaled copy
        is a GPU blit. The scale is anchored on the picture so it never
        stretches when the view's aspect changes; letterbox margins (and any
        subtitles in them) scale with it.
        """
        aspect = self._picture_aspect()
        if aspect is None:
            return False
        fbo = self._picture_fbo
        new_frame = self._ctx.update()
        if fbo is None or self._picture_due or new_frame:
            if fbo is None or not (
                    1 / _RESCALE_LIMIT <= width / fbo.width() <= _RESCALE_LIMIT
                    and 1 / _RESCALE_LIMIT <= height / fbo.height() <= _RESCALE_LIMIT):
                fbo = self._picture_fbo = QOpenGLFramebufferObject(QSize(width, height))
            self._ctx.render(
                flip_y=True,
                opengl_fbo={"fbo": fbo.handle(), "w": fbo.width(), "h": fbo.height()},
            )
            self._picture_due = False
        src_x, src_y, src_w, _src_h = _fit(fbo.width(), fbo.height(), aspect)
        dst_x, dst_y, dst_w, _dst_h = _fit(width, height, aspect)
        scale = dst_w / src_w
        left, bottom = dst_x - src_x * scale, dst_y - src_y * scale
        gl = QOpenGLContext.currentContext().extraFunctions()
        target = self.defaultFramebufferObject()
        gl.glBindFramebuffer(_GL_READ_FRAMEBUFFER, fbo.handle())
        gl.glBindFramebuffer(_GL_DRAW_FRAMEBUFFER, target)
        gl.glDisable(_GL_SCISSOR_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)
        gl.glBlitFramebuffer(
            0, 0, fbo.width(), fbo.height(),
            round(left), round(bottom),
            round(left + fbo.width() * scale), round(bottom + fbo.height() * scale),
            _GL_COLOR_BUFFER_BIT, _GL_LINEAR,
        )
        gl.glBindFramebuffer(_GL_FRAMEBUFFER, target)
        return True

    def _presenting(self) -> bool:
        window = self.window().windowHandle()
        return self.isVisible() and window is not None and window.isExposed()

    def _on_frame_ready(self) -> None:
        if self._ctx is None:
            return
        presenting = self._presenting()
        if self._report_display and presenting != (self._display_rate > 0):
            # Hidden or back: timing goes to audio and returns to the display
            # (see _report_display_rate). mpv switches in ~0.2 s either way.
            self._report_display_rate()
        if self._on_presenter:
            self._presenter.frame_ready()
            return
        if presenting:
            self.update()
            return
        # Wayland stops delivering paints on another workspace. Acknowledge
        # those frames through libmpv's render API instead of letting its VO
        # time out and count intentional non-presentation as dropped frames.
        # Even skip_rendering requires the owning OpenGL context to be current.
        self.makeCurrent()
        try:
            if QOpenGLContext.currentContext() == self.context() and self._ctx.update():
                self._ctx.render(skip_rendering=True)
        finally:
            self.doneCurrent()

    # -- HDR output ---------------------------------------------------------------
    #
    # mpv's render context is bound to one GL context, and freeing it while a
    # video plays tears the video down ("Video was forcefully killed"). It
    # moves between this widget and the HDR surface only in _prepare_surface,
    # which runs before every loadfile, while mpv is stopped and settled.

    @property
    def hdr_output(self) -> bool:
        """Whether the video plays on the PQ surface now."""
        return self._on_presenter and self._presenter is not None and self._presenter.pq

    @property
    def display_hdr(self) -> hdr_output.DisplayHdr:
        """The display's HDR state as read at the last load."""
        return self._display_hdr

    def set_hdr_output_enabled(self, enabled: bool) -> None:
        """The "Show HDR videos in HDR" setting. Applies at the next load;
        during an HDR video hdr_surface_stale asks for one."""
        self._hdr_enabled = bool(enabled)
        self._auto_hdr_refused = False  # the setting again: the compositor may switch now
        self._check_hdr_surface()

    def fullscreen_changed(self) -> None:
        """The window entered fullscreen (and settled) or left it: on an SDR
        screen whose compositor switches to HDR for a fullscreen HDR surface,
        an HDR video moves to the surface or back (one reload each way)."""
        self._check_hdr_surface()

    def _wanted_surface(self) -> tuple[bool, hdr_output.DisplayHdr]:
        """(play on the HDR surface?, the display's state). RELAY_HDR_SURFACE=
        force puts every video there, on any display (testing the surface and
        its overlays on an SDR screen); =off never does.

        In fullscreen on an SDR screen of a compositor that switches the
        screen to HDR for a fullscreen HDR surface (Hyprland), the surface is
        wanted too, with the HDR state the switch is expected to bring
        (``DisplayHdr.auto``). Out of fullscreen that HDR state goes, and the
        video with it, whatever the screen still reports.
        """
        mode = os.environ.get("RELAY_HDR_SURFACE", "").lower()
        if self.options.headless or mode == "off" or self._presenter_broken:
            return False, hdr_output.SDR
        if mode != "force" and not (
                self._hdr_enabled and self._source_hdr and hdr_output.presentation_supported()):
            return False, hdr_output.SDR
        window = self.window()
        display = hdr_output.query(window)
        if mode == "force":
            return True, display
        fullscreen = window is not None and window.isFullScreen()
        if display.hdr and (fullscreen or not self._auto_hdr):
            return True, display
        if fullscreen and not self._auto_hdr_refused and hdr_output.compositor_auto_hdr():
            return True, hdr_output.expected_hdr(display)
        return False, display

    @staticmethod
    def _pq_for(display: hdr_output.DisplayHdr) -> bool:
        return display.hdr and hdr_output.presentation_supported()

    async def _prepare_surface(self) -> None:
        """Put the render context where this video and display call for it."""
        was = self.hdr_output
        try:
            want, display = self._wanted_surface()
            self._display_hdr = display
            self._hdr_stale_reported = False
            if not want or not await self._move_to_presenter(display):
                self._move_to_widget()
            self._follow_auto_hdr(want, display)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - play on the widget instead
            self.log_message.emit("error", "relay", f"HDR surface: {error!r}")
            self._presenter_broken = True
            self._move_to_widget()
        self._apply_output_target()
        if self.hdr_output != was:
            self.hdr_output_changed.emit(self.hdr_output)

    def _follow_auto_hdr(self, want: bool, display: hdr_output.DisplayHdr) -> None:
        """Track the compositor's switch to HDR for the fullscreen surface."""
        if display.auto and self._on_presenter:
            self._auto_hdr = True
            if self._auto_hdr_deadline is None:
                self._auto_hdr_deadline = time.monotonic() + _AUTO_HDR_TIMEOUT_S
        elif not display.auto:
            self._auto_hdr_deadline = None  # it came, or is no longer awaited
        if not want and not display.hdr:
            # Out of fullscreen the screen reads HDR a moment longer: until
            # it reads SDR, that HDR stays the switch's, not the user's.
            self._auto_hdr = False

    async def _move_to_presenter(self, display: hdr_output.DisplayHdr) -> bool:
        pq = self._pq_for(display)
        presenter = self._presenter
        if presenter is not None and presenter.pq != pq:
            self._move_to_widget()
            self._discard_presenter()
            presenter = None
        top = self.window().windowHandle()
        if top is None:
            return False
        if presenter is None:
            surface = HdrPresenter
            if pq and hdr_output.platform_kind() == "windows":
                from .hdr_d3d11 import D3D11Presenter  # noqa: PLC0415 - Windows only
                surface = D3D11Presenter
            options = {"cover": hdr_output.platform_kind() == "wayland"} if surface is HdrPresenter else {}
            presenter = self._presenter = surface(
                self, top, pq=pq, sdr_white_nits=display.sdr_white_nits, peak_nits=display.peak_nits, **options)
            presenter.failed.connect(self._on_presenter_failed)
        presenter.sdr_white_nits = display.sdr_white_nits or presenter.sdr_white_nits
        presenter.peak_nits = display.peak_nits or presenter.peak_nits
        if self._on_presenter:
            return True
        presenter.follow_view()
        presenter.show()
        presenter.update()
        deadline = time.monotonic() + _HDR_SURFACE_TIMEOUT_S
        while not presenter.initialized:
            if self._presenter_broken or presenter.broken or time.monotonic() > deadline:
                presenter.hide()
                self.log_message.emit("warn", "relay", "HDR surface did not start; playing on the SDR surface")
                return False
            await asyncio.sleep(0.02)
        if self._presenter is not presenter:
            return False
        self._release_widget_ctx()
        presenter.makeCurrent()
        try:
            self._ctx = presenter.ctx = self._create_render_ctx()
        finally:
            presenter.doneCurrent()
        self._on_presenter = True
        self.update()  # black under the surface
        return True

    def _release_widget_ctx(self) -> None:
        if self._ctx is None or self._on_presenter:
            return
        self.makeCurrent()
        try:
            self._picture_fbo = None
            self._ctx.free()
            self._ctx = None
        finally:
            self.doneCurrent()

    def _move_to_widget(self) -> None:
        presenter = self._presenter
        if self._on_presenter and presenter is not None:
            presenter.release_render_context()
            self._ctx = None
            self._on_presenter = False
        if presenter is not None and presenter.isVisible():
            if hasattr(presenter, "release_description"):
                # Before the hide, whose commit applies it: Hyprland finds a
                # fullscreen window's HDR surface even hidden, and would keep
                # the screen in HDR for it.
                presenter.release_description()
            presenter.hide()
        context = self.context()
        if self._ctx is None and not self.options.headless and context is not None and context.isValid():
            self.makeCurrent()
            try:
                self._ctx = self._create_render_ctx()
            finally:
                self.doneCurrent()
        self.update()

    def _discard_presenter(self) -> None:
        presenter, self._presenter = self._presenter, None
        if presenter is not None:
            presenter.hide()
            if hasattr(presenter, "destroy_d3d"):
                presenter.destroy_d3d()
            if hasattr(presenter, "release_description"):
                presenter.release_description()
            presenter.deleteLater()

    def _render_ctx_lost(self, ctx) -> None:
        """The HDR surface's GL context went (window teardown) and freed ctx."""
        if self._ctx is ctx:
            self._ctx = None
            self._on_presenter = False

    def _on_presenter_failed(self, message: str) -> None:
        self.log_message.emit("error", "relay", message)
        self._presenter_broken = True
        if self._on_presenter and not self._hdr_stale_reported:
            self._hdr_stale_reported = True
            self.hdr_surface_stale.emit()

    def _release_surface_if_idle(self) -> None:
        # Deferred from stop(): a start or local load that follows at once
        # keeps the surface for its own decision (_prepare_surface).
        if self._on_presenter and self._task is None and not self._local_playback:
            self._move_to_widget()
            self._apply_output_target()
            self.hdr_output_changed.emit(False)

    def _apply_output_target(self, *, asynchronous: bool = False) -> None:
        """mpv's output colour: PQ for the HDR surface, mpv's own otherwise.
        vo_gpu converts to target-trc/prim and tone-maps to target-peak."""
        if self.options.headless:
            return
        pq = self.hdr_output
        peak = self._display_hdr.peak_nits if pq else None
        values = {
            "target-trc": "pq" if pq else "auto",
            "target-prim": "bt.2020" if pq else "auto",
            "target-peak": str(round(peak)) if peak else "auto",
        }
        # Only changes: an SDR load sends mpv nothing new.
        if values == self._output_target:
            return
        self._output_target = values
        for name, value in values.items():
            try:
                if asynchronous:
                    self.mpv.command_async("set", name, value)
                else:
                    self.mpv.command("set", name, value)
            except Exception as error:  # noqa: BLE001 - leave mpv's own value
                self.log_message.emit("warn", "relay", f"mpv {name}={value}: {error!r}")

    def _check_hdr_surface(self) -> None:
        """During playback, report a surface the display no longer fits
        (HDR switched on or off, the window on another screen, the setting,
        fullscreen on a screen the compositor switches to HDR)."""
        if self.options.headless or self._hdr_stale_reported:
            return
        if self._task is None and not self._local_playback:
            return
        params = self._observed.get("video-params") or {}
        if not self._source_hdr and self._epoch_released and params.get("gamma") in ("pq", "hlg"):
            # The stream turned out HDR though the session said otherwise (an
            # HLG alternative-transfer SEI over BT.2020 tags, PROTOCOL §3.5):
            # mpv's reading of the frames wins, and one reload moves it over.
            self._source_hdr = True
        if not (self._source_hdr or self._on_presenter):
            return
        want, display = self._wanted_surface()
        if display.auto and self._auto_hdr_deadline is not None and time.monotonic() > self._auto_hdr_deadline:
            # The compositor kept the screen in SDR (a no_auto_hdr window
            # rule, say): the video goes back to the widget, tone-mapped.
            self._auto_hdr_refused = True
            self.log_message.emit("warn", "relay", "The display did not switch to HDR in fullscreen; "
                                                   "playing the HDR video tone-mapped")
            want, display = self._wanted_surface()
        self._follow_auto_hdr(want, display)
        fits = want == self._on_presenter and (
            not want or (self._presenter is not None and self._presenter.pq == self._pq_for(display)))
        if not fits:
            self._hdr_stale_reported = True
            self.hdr_surface_stale.emit()
        elif want and display != self._display_hdr:
            # Same surface, other luminances (a brightness setting, or the
            # compositor's switch to HDR the surface was waiting for): adopt
            # them. mpv takes its new target-peak live.
            self._display_hdr = display
            if display.sdr_white_nits:
                self._presenter.sdr_white_nits = display.sdr_white_nits
            if display.peak_nits:
                self._presenter.peak_nits = display.peak_nits
            self._presenter.update()  # composes, and describes the surface, anew
            self._apply_output_target(asynchronous=True)
            self.hdr_output_changed.emit(True)  # the hint's peak

    # -- public API -----------------------------------------------------------

    def start(self, session, downlink_q: asyncio.Queue, time_base: Fraction,
              source_path: str | None = None, avg_rate: Fraction | None = None,
              source_has_audio: bool = True, *, paused: bool = False,
              remembered_tracks: dict | None = None) -> None:
        self.stop()
        if session.downlink_container != "matroska":
            self.failed.emit(f"unsupported downlink container: {session.downlink_container}")
            return
        self._fps = float(avg_rate) if avg_rate else 30.0
        self._downlink_codec = getattr(session, "downlink_codec", None)
        self._source_hdr = bool(getattr(session, "downlink_hdr", False))
        self._source_path = source_path
        self._source_has_audio = source_has_audio
        self._tracks_reported = False
        self._chosen_subtitle_id = None
        self._subtitle_choice_made = False
        self._chosen_audio_id = None
        self._audio_choice_made = False
        self._caller_paused = paused
        self._remembered_tracks = dict(remembered_tracks or {})
        def explicit(kind):
            return kind in self._remembered_tracks and (
                self._remembered_tracks[kind] is None or self._remembered_tracks[kind].get("explicit", True))
        self._subtitle_choice_made = explicit("sub")
        self._audio_choice_made = explicit("audio")
        self._cached_tracks = []
        self._relay_eos_epoch = None
        self._transport_failed = False
        self._task = asyncio.create_task(self._consume(downlink_q))
        self._stats_task = asyncio.create_task(self._stats_loop())

    async def _load_stream(self) -> None:
        """(Re)start mpv on a fresh buffer — session start and every seek.

        prepare_seek() normally retires the old file immediately, before the
        server seek. We still preserve the stop/load settle interval here:
        doing them back-to-back raced mpv's event thread and crashed natively.
        """
        if self._buffer is not None:
            self._retire_stream()
        settle = self._reload_settle_until - time.monotonic()
        if settle > 0:
            await asyncio.sleep(settle)
        await self._prepare_surface()
        self._relay_eos_epoch = None
        self._buffer = _LoopbackStream()
        self._load_generation += 1
        self._fed = 0
        # A seek is a new Matroska file. Enumerate its tracks and remap cached
        # descriptors before releasing the epoch; numeric IDs can change.
        self._tracks_reported = False
        self._restart_seen = False
        self._external_attach_started = False
        self._external_ready = self._source_path is None
        self._prebuffer_ready = False
        self._epoch_released = False
        self._accept_playback_restart = True
        # No `start=` option: with rebase-start-time=no the stream's own
        # timestamps place playback at the seek target. External media is
        # attached after playback-restart, when mpv knows that absolute time.
        self._pending_start = None
        # This localhost pipe deliberately goes silent during a user pause or
        # server watermark hold. mpv's default 60-second TCP read timeout turns
        # that silence into EOF, which ends playback as soon as its cache drains
        # after resume. Scope the override to relay epochs; ordinary local/HTTP
        # playback retains the user's network timeout. Transport errors and
        # stop/seek still close the pipe explicitly.
        load_options = "pause=yes,network-timeout=0"
        if self._downlink_codec == "ffv1" and self.mpv.mpv_version_tuple < (0, 38, 0):
            # New FFmpeg muxers use V_FFV1. mpv's native Matroska demuxer
            # learned that tag in 0.38; older libmpv can decode it through
            # libavformat. Scope this compatibility override to relay epochs.
            load_options += ",demuxer=lavf,demuxer-lavf-format=matroska"
        if self.mpv.mpv_version_tuple >= (0, 38, 0):
            self.mpv.command(
                "loadfile", self._buffer.uri, "replace", -1, load_options)
        else:
            self.mpv.command(
                "loadfile", self._buffer.uri, "replace", load_options)
        self._reloading = False

    def set_subtitle_fonts_dir(self, path: Path | None) -> None:
        """Point libass at the verified per-session attachment view."""
        self.mpv.sub_fonts_dir = (
            str(path) if path is not None else self._default_sub_fonts_dir
        )

    def _retire_stream(self) -> None:
        """Stop and discard an epoch without waiting for its replacement."""
        self._accept_playback_restart = False
        self._load_generation += 1  # invalidates a late external-media worker
        self._reloading = True
        old = self._buffer
        self._buffer = None
        if old is not None:
            old.abort()
        try:
            self.mpv.command("stop")
        except Exception:
            pass
        self._reload_settle_until = time.monotonic() + 0.15
        self._relay_eos_epoch = None
        self._fed = 0
        self._restart_seen = False
        self._external_ready = False
        self._prebuffer_ready = False
        self._epoch_released = False
        if self.client is not None:
            self.client.buffered_ms = 0

    def _on_playback_restarted(self, generation: int) -> None:
        if (generation != self._load_generation
                or not self._accept_playback_restart or self._restart_seen):
            return
        self._restart_seen = True
        if self._source_path is None:
            self._external_ready = True
            self._maybe_release_epoch()
            return
        if self._external_attach_started:
            return
        self._external_attach_started = True
        source = self._source_path
        source_has_audio = self._source_has_audio
        chosen_audio = self._chosen_audio_id
        chosen_subtitle = self._chosen_subtitle_id
        subtitle_choice_made = self._subtitle_choice_made

        def attach() -> None:
            error: Exception | None = None
            try:
                if generation != self._load_generation:
                    return
                # Add the original once, after the live stream has established
                # its absolute PTS. mpv exposes all of that demuxer's audio and
                # subtitle tracks; adding it twice needlessly opens/parses the
                # same file twice and materially slows every load.
                # mpv rejects audio-add when a file has only subtitles. Use
                # the corresponding sub-add once for that metadata-confirmed
                # case; it still opens one original demuxer after restart.
                select = (
                    chosen_audio is None if source_has_audio
                    else chosen_subtitle is None and not subtitle_choice_made
                )
                self.mpv.command(
                    "audio-add" if source_has_audio else "sub-add", source,
                    "select" if select else "auto",
                )

                # Unlike Android's MediaCodec build, desktop mpv does not
                # reliably publish audio-pts while pause=yes holds the epoch.
                # Waiting for it is circular and intermittently strands the
                # first frame. audio-add itself returns only after the original
                # demuxer is open/positioned; release then and let mpv's normal
                # cache/audio-sync path finish decoding.
            except Exception as err:
                error = err
            if generation == self._load_generation:
                if error is not None:
                    self._terminal.emit(generation, False, f"external media attach: {error!r}")
                self._external_media_ready.emit(generation)

        threading.Thread(
            target=attach,
            name=f"mpv-external-media-{generation}",
            daemon=True,
        ).start()

    def _on_external_media_ready(self, generation: int) -> None:
        if generation != self._load_generation:
            return
        self._external_ready = True
        self._maybe_release_epoch()

    def _maybe_release_epoch(self) -> None:
        if (self._epoch_released or not self._restart_seen
                or not self._external_ready or not self._prebuffer_ready):
            return
        self._autoselect_tracks(self.mpv.track_list or [])
        self._epoch_released = True
        self.mpv.pause = self._caller_paused

    def stop(self) -> None:
        self._idle_inhibitor.set_active(False)
        self._reloading = True  # suppress EOF caused by retiring our own pipe
        self._reload_settle_until = time.monotonic() + 0.15
        ready = self._local_load_ready
        self._local_load_ready = None
        self._pending_local_seek = None
        if ready is not None and not ready.done():
            ready.cancel()
        for task in (self._task, self._stats_task):
            if task is not None:
                task.cancel()
        self._task = self._stats_task = None
        self._accept_playback_restart = False
        self._load_generation += 1
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        try:
            self.mpv.command("stop")
        except Exception:
            pass
        self._epoch_released = False
        self._caller_paused = False
        self._local_playback = False
        if self._on_presenter:
            QTimer.singleShot(0, self._release_surface_if_idle)

    def set_paused(self, paused: bool) -> None:
        self._caller_paused = paused
        # Each epoch is deliberately held until mpv reaches its first frame
        # and external audio (if any) catches up. Preserve user intent without
        # prematurely releasing that hold.
        if self._epoch_released:
            self.mpv.pause = paused

    def set_panscan(self, value: float) -> None:
        """0.0 = fit (letterbox); 1.0 = fill the window, cropping overflow.

        A global mpv property, so it persists across the stop/loadfile of a
        seek reload — set it once and it holds for the session. In "cover"
        mode the server already sizes the video to cover the display, so
        panscan=1 crops exactly the overflow to native pixels."""
        try:
            self.mpv.panscan = value
        except Exception:
            pass

    def set_deband(self, enabled: bool) -> None:
        """Toggle mpv's GPU output debander after hardware decode."""
        try:
            self.mpv.deband = bool(enabled)
        except Exception:
            pass

    def prepare_seek(self, target_s: float) -> None:
        """Discard the old epoch before the relay starts building the new one."""
        self._pending_start = target_s
        self._retire_stream()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if not event.modifiers():
            if key == Qt.Key_Space:
                self.pause_requested.emit()
                return
            if key == Qt.Key_F:
                self.fullscreen_toggled.emit()
                return
            if key in self._SEEK_KEYS:
                self.seek_requested.emit(self._SEEK_KEYS[key])
                return
            if key in self._CHAPTER_KEYS:
                self.chapter_step_requested.emit(self._CHAPTER_KEYS[key])
                return
        mpv_key = _mpv_key_name(event)
        if mpv_key is not None and mpv_key not in self._BLOCKED_KEYS:
            try:
                self.mpv.keypress(mpv_key)
            except Exception:
                pass
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.fullscreen_toggled.emit()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        self.mouse_moved.emit(pos.x(), pos.y())
        super().mouseMoveEvent(event)

    def apply_defaults(self, values: dict[str, str]) -> None:
        """Apply only the exposed properties, never reload config into a stream."""
        self._defaults.update(values)
        for name, value in values.items():
            if name == "sid" and self._subtitle_choice_made:
                continue  # a current-file choice wins over global defaults
            self.mpv.command("set", name, value)
        if ("slang" in values or "sid" in values) and not self._subtitle_choice_made:
            self._remembered_tracks.pop("sub", None)
            self._tracks_reported = False
        if "slang" in values and not self._subtitle_choice_made:
            self.mpv.command("set", "sid", self._defaults.get("sid", "auto"))

    def track_choices(self) -> dict:
        flags = {"sub": self._subtitle_choice_made, "audio": self._audio_choice_made}
        return {kind: ({**descriptor, "explicit": flags[kind]} if descriptor is not None else None)
                for kind, descriptor in self._remembered_tracks.items()}

    def _autoselect_tracks(self, tracks: list) -> None:
        self._cached_tracks = [dict(t) for t in tracks]
        remembered = self._remembered_tracks
        for kind, prop in (("audio", "aid"), ("sub", "sid")):
            if kind in remembered:
                descriptor = remembered[kind]
                selected = match_track(descriptor, tracks) if descriptor is not None else "no"
                if selected is not None:
                    self.mpv.command("set", prop, str(selected))
            elif kind == "sub":
                # Native mpv language/default selection, including subtitle off.
                self.mpv.command("set", "sid", self._defaults.get("sid", "auto"))
        # Capture automatically chosen tracks too, so an epoch reload preserves
        # the same descriptor even when the new stream assigns different IDs.
        for kind, prop in (("audio", "aid"), ("sub", "sid")):
            selected = getattr(self.mpv, prop)
            track = next((t for t in tracks if t.get("type") == kind and t.get("id") == selected), None)
            if track is not None:
                remembered[kind] = track_descriptor(track, tracks)
            if kind == "sub":
                self._chosen_subtitle_id = selected if type(selected) is int else None
            else:
                self._chosen_audio_id = selected if type(selected) is int else None

    def select_audio(self, aid: int) -> None:
        track = next((t for t in self._cached_tracks if t.get("type") == "audio" and t.get("id") == aid), None)
        if track is not None:
            self._remembered_tracks["audio"] = track_descriptor(track, self._cached_tracks)
        self._chosen_audio_id = aid
        self._audio_choice_made = True
        self.mpv.aid = aid

    def select_subtitle(self, sid: int | None) -> None:
        track = next((t for t in self._cached_tracks if t.get("type") == "sub" and t.get("id") == sid), None)
        if sid is None or track is not None:
            self._remembered_tracks["sub"] = track_descriptor(track, self._cached_tracks) if track else None
        self._chosen_subtitle_id = sid
        self._subtitle_choice_made = True
        self.mpv.sid = sid if sid is not None else "no"

    def delay_state(self):
        return self._cached_audio_delay, self._cached_sub_delay

    def set_sub_delay(self, seconds: float) -> None:
        self._cached_sub_delay = seconds
        self.mpv.sub_delay = seconds

    def set_audio_delay(self, seconds: float) -> None:
        self._cached_audio_delay = seconds
        self.mpv.audio_delay = seconds

    def audio_output_state(self) -> tuple[int, bool]:
        return max(0, round(self.mpv.volume or 0)), bool(self.mpv.mute)

    def set_volume(self, percent: int) -> None:
        self.mpv.volume = max(0, percent)

    def set_muted(self, muted: bool) -> None:
        self.mpv.mute = bool(muted)

    async def play_local(
        self, path: str, position_s: float = 0.0, *, paused: bool = False, hdr: bool = False,
    ) -> None:
        """Return once the original file accepts transport controls.

        loadfile only queues a native load. Hold playback until its file-loaded
        event arrives, preserving pause/seek requests made during the load.
        ``hdr``: the file is PQ or HLG (it may play on the HDR surface).
        """
        self.stop()
        self._source_hdr = bool(hdr)
        generation = self._load_generation
        loop = asyncio.get_running_loop()
        self._local_load_ready = ready = loop.create_future()
        self._reloading = True
        self.client = None
        self._source_path = str(path)
        self._local_playback = True
        self._tracks_reported = False
        self._caller_paused = paused
        callback = None

        def complete(error: str | None) -> None:
            if generation != self._load_generation or ready.done():
                return
            if error is None:
                ready.set_result(None)
            else:
                ready.set_exception(RuntimeError(error))

        try:
            # The native stop/load settle interval also applies when leaving
            # a relay stream for its original file. No property reads here.
            await asyncio.sleep(0.15)
            if generation != self._load_generation:
                return
            await self._prepare_surface()
            if generation != self._load_generation:
                return
            entry_id = None

            @self.mpv.event_callback("start-file", "file-loaded", "end-file")
            def callback(event):
                nonlocal entry_id
                # Event data belongs to libmpv. Copy primitive fields before
                # scheduling back onto asyncio; never block the event thread.
                kind = event.event_id.value
                if kind == mpv.MpvEventID.START_FILE:
                    entry_id = event.data.playlist_entry_id
                    return
                if entry_id is None:
                    return
                if kind == mpv.MpvEventID.FILE_LOADED:
                    error = None
                elif (event.data.playlist_entry_id == entry_id
                      and event.data.reason != mpv.MpvEventEndFile.REDIRECT):
                    error = f"Local media load ended before it was ready (mpv error {event.data.error})"
                else:
                    return
                try:
                    loop.call_soon_threadsafe(complete, error)
                except RuntimeError:
                    pass  # the application loop was closed during shutdown

            self.mpv.loadfile(
                self._source_path, start=str(max(0.0, position_s)), pause="yes",
            )
            async with asyncio.timeout(30):
                await ready
            if generation != self._load_generation:
                return
            if self._pending_local_seek is not None:
                self.mpv.command("seek", self._pending_local_seek, "absolute+exact")
                self._pending_local_seek = None
            self._epoch_released = True
            self.mpv.pause = self._caller_paused
            self._reloading = False
            self._stats_task = asyncio.create_task(self._stats_loop())
        except BaseException as error:
            if generation == self._load_generation:
                self.stop()
            if isinstance(error, TimeoutError):
                raise RuntimeError("Timed out waiting for local media to load") from error
            raise
        finally:
            if callback is not None:
                self.mpv.unregister_event_callback(callback)
            if self._local_load_ready is ready:
                self._local_load_ready = None

    def seek_local(self, target_s: float) -> None:
        """Seek the original file directly, retaining its absolute timeline."""
        if self._local_playback:
            target_s = max(0.0, target_s)
            if self._epoch_released:
                self.mpv.command("seek", target_s, "absolute+exact")
            else:
                self._pending_local_seek = target_s

    # -- internals ---------------------------------------------------------------

    async def _consume(self, q: asyncio.Queue) -> None:
        import sys as _sys

        trace = self.options.trace
        try:
            await self._load_stream()  # initial stream open on the consume task
            first = True
            while True:
                pkt = await q.get()
                if pkt is None:
                    self._transport_failed = True
                    self.failed.emit("downlink closed")
                    return
                # A receiver batch can have been waiting to publish while a
                # seek drained its queue. Recheck at the final consumer so an
                # old header, payload, or EOF cannot affect the current load.
                if self.client is not None and pkt.epoch != self.client.epoch:
                    continue
                if pkt.eos:
                    self._relay_eos_epoch = pkt.epoch
                    if self._buffer is not None:
                        self._buffer.finish()  # mpv plays out and emits eof
                    self._prebuffer_ready = True
                    self._maybe_release_epoch()
                    # EOS closes this epoch's bytes, which can arrive well
                    # before mpv finishes playing (or while it is paused).
                    # Keep consuming so a later seek can load a fresh epoch.
                    continue
                if pkt.discontinuity and (not first or self._buffer is None):
                    if trace:
                        print("[trace] discontinuity -> reload", flush=True, file=_sys.stderr)
                    # Seek: fresh container stream -> reload mpv on a new buffer.
                    await self._load_stream()
                    if self.client is not None and pkt.epoch != self.client.epoch:
                        continue  # another seek arrived during the native settle interval
                    if trace:
                        print("[trace] reload done", flush=True, file=_sys.stderr)
                first = False
                if pkt.payload and self._buffer is not None:
                    self._buffer.feed(pkt.payload)
                    self._fed += 1
                    if trace and self._fed % 48 == 0:
                        print(
                            f"[trace] fed={self._fed} released={self._epoch_released}",
                            flush=True, file=_sys.stderr,
                        )
                if not self._prebuffer_ready and self._fed >= self.STARTUP_PACKETS:
                    self._prebuffer_ready = True
                    if trace:
                        print(
                            f"[trace] mpv owns buffering after {self._fed} packet(s)",
                            flush=True, file=_sys.stderr,
                        )
                    self._maybe_release_epoch()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.failed.emit(f"mpv feed: {err!r}")

    async def _stats_loop(self) -> None:
        was_buffering = False
        ticks = 0
        while True:
            await asyncio.sleep(0.5)
            ticks += 1
            if ticks % _HDR_RECHECK_TICKS == 0 or self._auto_hdr_deadline is not None:
                self._check_hdr_surface()  # each tick while the switch to HDR is awaited
            if self._reloading:
                if self.client is not None:
                    self.client.buffered_ms = 0
                continue
            # Observed values (see _OBSERVED_PROPERTIES), not synchronous
            # reads. They stay live while paused: buffered_ms must keep
            # following the real buffer, because the server paces on the
            # reported value and a frozen stale report wedges its
            # backpressure pause while the real buffer drains.
            _prop = self._observation
            pos = _prop("time-pos")
            avsync = _prop("avsync")
            drop = _prop("frame-drop-count")
            cache = _prop("demuxer-cache-duration")
            buffering = bool(_prop("paused-for-cache"))
            output = (max(0, round(_prop("volume", 100) or 0)), bool(_prop("mute")))
            if output != self._last_audio_output:
                self._last_audio_output = output
                self.volume_changed.emit(*output)
            mpv_buffered_ms = int((cache or 0) * 1000)
            buffer_stats = self._buffer.stats() if self._buffer is not None else {
                "chunks": 0, "queued_bytes": 0,
            }
            receive_stats = self.client.downlink_stats() if self.client is not None else {
                "mbps": 0.0, "queue_packets": 0,
            }
            # Packets waiting in the bridge or loopback stream are already
            # buffered client-side even though mpv's demux cache cannot see
            # them. Include their approximate PTS duration in buffer_report;
            # otherwise the server free-runs while hundreds of MiB accumulate
            # immediately before mpv.
            pre_mpv_packets = receive_stats["queue_packets"] + buffer_stats["chunks"]
            pre_mpv_ms = int(pre_mpv_packets / max(1.0, self._fps) * 1000)
            buffered_ms = mpv_buffered_ms + pre_mpv_ms
            if self.client is not None:
                self.client.buffered_ms = buffered_ms
            stable = self._epoch_released and not self._reloading and not bool(_prop("seeking", False))
            if stable:
                self._cached_audio_delay = float(_prop("audio-delay", 0) or 0)
                self._cached_sub_delay = float(_prop("sub-delay", 0) or 0)
                if self._tracks_reported:
                    for kind, prop, cached in (("audio", "aid", self._chosen_audio_id), ("sub", "sid", self._chosen_subtitle_id)):
                        selected = _prop(prop)
                        selected = selected if type(selected) is int else None
                        if selected != cached:
                            track = next((t for t in self._cached_tracks if t.get("type") == kind and t.get("id") == selected), None)
                            if selected is None or track:
                                self._remembered_tracks[kind] = track_descriptor(track, self._cached_tracks) if track else None
                                if kind == "sub":
                                    self._chosen_subtitle_id = selected
                                    self._subtitle_choice_made = True
                                else:
                                    self._chosen_audio_id = selected
                                    self._audio_choice_made = True
            self.telemetry_changed.emit(PlaybackTelemetry(
                float(pos) if pos is not None else None, _prop("duration"), buffered_ms,
                receive_stats["mbps"], drop or 0, avsync or 0.0, stable,
            ))
            if buffering != was_buffering:
                was_buffering = buffering
                self.rebuffering.emit(buffering)
            if pos is not None:
                hwdec = _prop("hwdec-current") or "sw"
                loopback_mib = buffer_stats["queued_bytes"] / (1024 * 1024)
                self.position_changed.emit(float(pos))
                self.stats_changed.emit(
                    f"pos {pos:6.1f}s | total {buffered_ms:5d} ms "
                    f"(mpv {mpv_buffered_ms}, pre {pre_mpv_ms}) | "
                    f"rx {receive_stats['mbps']:5.0f} Mbps q{receive_stats['queue_packets']} | "
                    f"loopback {loopback_mib:5.0f} MiB/{buffer_stats['chunks']} | "
                    f"hw {hwdec} | drift {avsync if avsync is not None else 0:+.3f}s | "
                    f"dropped {drop or 0}"
                )
            # During an epoch reload mpv can retain the old file's track-list
            # through stop() and briefly after loadfile(). Publishing it here
            # makes the UI show stale ids while the new file independently
            # auto-selects another track. The path changes to the per-epoch
            # loopback URI only once mpv has adopted the fresh Matroska file.
            current_uri = (
                self._source_path if self._local_playback
                else self._buffer.uri if self._buffer is not None else None
            )
            tracks_belong_to_current_epoch = (
                current_uri is not None
                and not self._reloading
                and _prop("path") == current_uri
                and (self._local_playback or self._source_path is None or self._external_ready)
            )
            if not self._tracks_reported and tracks_belong_to_current_epoch:
                tracks = self.mpv.track_list or []
                audio = [(t.get("id"), t.get("title") or t.get("lang") or f"track {t.get('id')}")
                         for t in tracks if t.get("type") == "audio"]
                subs = [(t.get("id"), t.get("title") or t.get("lang") or f"track {t.get('id')}")
                        for t in tracks if t.get("type") == "sub"]
                if tracks:
                    self._tracks_reported = True
                    self._autoselect_tracks(tracks)
                    aid = self.mpv.aid
                    if not self._audio_choice_made and type(aid) is int:
                        self._chosen_audio_id = aid
                    self.audio_track_list_changed.emit(
                        audio, aid if type(aid) is int else None)
                    sid = self.mpv.sid
                    if not self._subtitle_choice_made and type(sid) is int:
                        self._chosen_subtitle_id = sid
                    self.track_list_changed.emit(
                        subs, sid if type(sid) is int else None)
