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
import threading
import time
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .options import DesktopOptions


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
                    sock.close()
                except OSError:
                    pass

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
    stats_changed = Signal(str)
    position_changed = Signal(float)  # seconds
    track_list_changed = Signal(list, object)  # [(sid, title)] subs, selected sid
    audio_track_list_changed = Signal(list, object)  # [(aid, title)] audio, selected aid
    rebuffering = Signal(bool)
    seek_requested = Signal(float)  # relative seconds (arrow keys)
    chapter_step_requested = Signal(int)  # +1 next / -1 previous (PgUp/PgDn)
    finished = Signal()
    failed = Signal(str)
    fullscreen_toggled = Signal()  # F key / double-click; the window fullscreens
    mouse_moved = Signal(int, int)  # cursor x, y in the view; drives fullscreen control reveal
    _frame_ready = Signal()  # mpv render thread -> queued repaint on GUI thread
    _playback_restarted = Signal(int)  # libmpv event thread -> GUI thread
    _external_media_ready = Signal(int)  # attach worker -> GUI thread

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
        # mpv renders through the libmpv render API into this QOpenGLWidget's
        # framebuffer (initializeGL/paintGL below) — no native child window.
        # The old wid embed needed a real X11 window: on Wayland it forced
        # XWayland (broken HiDPI) or popped a separate mpv window, and the
        # native subsurface drew sibling widgets duplicated/shifted.
        self.setMinimumSize(480, 270)
        self.setFocusPolicy(Qt.StrongFocus)  # receive keys for mpv forwarding
        self.setMouseTracking(True)  # deliver mouse-move without a pressed button
        self._ctx = None  # MpvRenderContext, created in initializeGL
        self._get_proc = None  # ctypes callback — must outlive the render ctx
        self._frame_ready.connect(self.update)  # queued: emitter is mpv thread

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
                # Respect the user's mpv.conf/input.conf (libmpv loads no
                # config by default). The config file is parsed during init
                # and OVERRIDES constructor options — everything the relay
                # depends on is re-asserted post-init below. User scripts
                # stay off: LuaJIT scripts hit the same stream-reload
                # instability as the stock OSC (--mpv-scripts opts in).
                "config": "yes",
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
            }
            # Hardware decode (NVDEC/D3D11 on Windows, VAAPI on Linux) for the
            # HEVC tiers; FFV1 has no hw decoder and mpv falls back silently.
            # The earlier hwdec crash was an OSC(LuaJIT)-reload interaction —
            # with the OSC off, seek batteries run clean with hwdec on.
            # --no-hwdec disables.
            if not self.options.no_hwdec:
                extra["hwdec"] = "auto-safe"
            # OSC is OFF by default: it's a LuaJIT script that re-initializes
            # on every stream reload (seek), and that path intermittently
            # crashed mpv's event thread (native AV, exception 0xe24c4a02 =
            # LuaJIT) on a cold/slow pipeline. Our Qt controls cover the same
            # ground. --mpv-osc re-enables it for anyone who wants the
            # native overlay and rarely seeks. NB: on the render-API path mpv
            # has no window, so the OSC is display-only (no mouse input).
            if not self.options.mpv_scripts:
                extra["load_scripts"] = "no"
            if self.options.mpv_osc:
                extra["osc"] = "yes"
                extra.update({
                    "input_default_bindings": "yes",
                    "input_vo_keyboard": "yes",
                    "input_cursor": "yes",
                })
        self.mpv = mpv.MPV(
            log_handler=None,
            loglevel="error",
            keep_open="no",
            idle="yes",
            **extra,
        )
        self._default_sub_fonts_dir = getattr(self.mpv, "sub_fonts_dir", "")
        if "config" in extra:
            # mpv.conf won over any constructor option it named; re-assert
            # the plumbing the relay breaks without (runtime sets beat the
            # config file). User prefs — hwdec, shaders, volume, subtitle
            # style, screenshots — stand.
            self.mpv.vo = "libmpv"  # render API; a conf vo= would pop a window
            self.mpv.rebase_start_time = False  # docs/PROTOCOL.md PTS semantics
            self.mpv.keep_open = False
            self.mpv.idle = True
            self.mpv.cache = True  # live-stream buffering, sized for the
            self.mpv.cache_pause = True
            self.mpv.cache_pause_wait = CACHE_PAUSE_WAIT_S
            self.mpv.demuxer_readahead_secs = 15  # ~200 Mbps lossless tiers
            self.mpv.demuxer_max_bytes = "768MiB"
            self.mpv.demuxer_max_back_bytes = 0
            if self.options.no_hwdec:
                self.mpv.hwdec = "no"
            if not self.options.mpv_osc:
                self.mpv.osc = False
        self._buffer: _LoopbackStream | None = None
        self._task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._source_path: str | None = None
        self._fps = 30.0
        self._fed = 0
        self._pending_start: float | None = None  # seek target for next reload
        self._epoch_base = 0  # frames fed before the current epoch's stream
        self._tracks_reported = False
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
        self._playback_restarted.connect(self._on_playback_restarted)
        self._external_media_ready.connect(self._on_external_media_ready)

        @self.mpv.event_callback("playback-restart")
        def on_restart(_event):
            # libmpv invokes callbacks on its event thread. Hop back to Qt's
            # thread before changing properties or starting the attach worker.
            if self._accept_playback_restart:
                self._playback_restarted.emit(self._load_generation)

        @self.mpv.event_callback("end-file")
        def on_end(event):
            reason = str(getattr(event.data, "reason", ""))
            # Suppress the EOF that our own reload produces when it closes the
            # old buffer — only a true end-of-content should end the session.
            if self._reloading:
                return
            if reason in ("MpvEventEndFile.EOF", "eof", "0"):
                self.finished.emit()

    # -- rendering (libmpv render API) ------------------------------------------

    def initializeGL(self) -> None:
        if self.options.headless or self._ctx is not None:
            return
        self._get_proc = mpv.MpvGlGetProcAddressFn(_get_proc_address)
        render_params = {
            "opengl_init_params": {"get_proc_address": self._get_proc},
            **_native_display_params(),
        }
        self._ctx = mpv.MpvRenderContext(
            self.mpv, "opengl",
            **render_params,
        )
        self._ctx.update_cb = self._frame_ready.emit
        # The GL context is destroyed before the widget on teardown — free the
        # render context first, while the GL context is still alive.
        self.context().aboutToBeDestroyed.connect(self._free_render_ctx)

    def _free_render_ctx(self) -> None:
        if self._ctx is not None:
            self.makeCurrent()
            self._ctx.free()
            self._ctx = None
            self.doneCurrent()

    def paintGL(self) -> None:
        if self._ctx is None:
            return
        # QOpenGLWidget's backing FBO is in physical pixels (HiDPI-scaled).
        dpr = self.devicePixelRatioF()
        self._ctx.render(
            flip_y=True,
            opengl_fbo={
                "fbo": self.defaultFramebufferObject(),
                "w": round(self.width() * dpr),
                "h": round(self.height() * dpr),
            },
        )

    # -- public API -----------------------------------------------------------

    def start(self, session, downlink_q: asyncio.Queue, time_base: Fraction,
              source_path: str | None = None, avg_rate: Fraction | None = None) -> None:
        self.stop()
        if session.downlink_container != "matroska":
            self.failed.emit(f"unsupported downlink container: {session.downlink_container}")
            return
        self._fps = float(avg_rate) if avg_rate else 30.0
        self._source_path = source_path
        self._tracks_reported = False
        self._chosen_subtitle_id = None
        self._subtitle_choice_made = False
        self._chosen_audio_id = None
        self._audio_choice_made = False
        self._caller_paused = False
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
        self._buffer = _LoopbackStream()
        self._load_generation += 1
        self._fed = 0
        # A seek is a new Matroska file. Embedded auxiliary tracks get fresh
        # mpv ids from an identical deterministic header, so enumerate them
        # again and reapply the user's explicit subtitle choice.
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
        load_options = "pause=yes"
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
        chosen_audio = self._chosen_audio_id
        chosen_subtitle = self._chosen_subtitle_id
        subtitle_choice_made = self._subtitle_choice_made

        def attach() -> None:
            error: Exception | None = None
            try:
                # Add the original once, after the live stream has established
                # its absolute PTS. mpv exposes all of that demuxer's audio and
                # subtitle tracks; adding it twice needlessly opens/parses the
                # same file twice and materially slows every load.
                self.mpv.command(
                    "audio-add", source,
                    "select" if chosen_audio is None else "auto",
                )
                if chosen_audio is not None:
                    self.mpv.aid = chosen_audio
                if subtitle_choice_made:
                    self.mpv.sid = (
                        chosen_subtitle if chosen_subtitle is not None else "no")
                elif chosen_subtitle is not None:
                    self.mpv.sid = chosen_subtitle

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
                    self.failed.emit(f"external audio attach: {error!r}")
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
        self._epoch_released = True
        self.mpv.pause = self._caller_paused

    def stop(self) -> None:
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
        self._reloading = False
        self._epoch_released = False
        self._caller_paused = False

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

    def _autoselect_tracks(self, tracks: list) -> None:
        """Subs on by default: external sub-files tracks carry no default
        flag, so mpv selects none. Prefer the source's default track, else
        the first. Same fallback for audio (mpv sometimes selects none on
        live-stream loads)."""
        if self._subtitle_choice_made:
            self.mpv.sid = (
                self._chosen_subtitle_id
                if self._chosen_subtitle_id is not None else "no"
            )
        elif self._chosen_subtitle_id is not None:
            # Preserve the initially auto-selected track across epochs too.
            # mpv's own default choice can change when a seek begins between
            # subtitle packets even though every stream remains in the header.
            self.mpv.sid = self._chosen_subtitle_id
        elif self.mpv.sid in (None, False, "no"):
            subs = [t for t in tracks if t.get("type") == "sub"]
            if subs:
                pick = next((t for t in subs if t.get("default")), subs[0])
                self.mpv.sid = pick["id"]
        if self._audio_choice_made:
            self.mpv.aid = self._chosen_audio_id
        elif self._chosen_audio_id is not None:
            self.mpv.aid = self._chosen_audio_id
        elif self.mpv.aid in (None, False, "no"):
            audio = [t for t in tracks if t.get("type") == "audio"]
            if audio:
                self.mpv.aid = audio[0]["id"]

    def select_audio(self, aid: int) -> None:
        self._chosen_audio_id = aid
        self._audio_choice_made = True
        self.mpv.aid = aid

    def select_subtitle(self, sid: int | None) -> None:
        self._chosen_subtitle_id = sid
        self._subtitle_choice_made = True
        self.mpv.sid = sid if sid is not None else "no"

    def set_sub_delay(self, seconds: float) -> None:
        self.mpv.sub_delay = seconds

    def set_audio_delay(self, seconds: float) -> None:
        self.mpv.audio_delay = seconds

    def play_local_fallback(self, position_s: float) -> None:
        """Direct playback of the original file (server lost)."""
        for task in (self._task, self._stats_task):
            if task is not None:
                task.cancel()
        self._task = None
        if self._buffer is not None:
            self._buffer.close()
        self.mpv.pause = False
        self.mpv.loadfile(self._source_path, start=str(position_s))

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
                    self.failed.emit("downlink closed")
                    return
                if pkt.eos:
                    if self._buffer is not None:
                        self._buffer.finish()  # mpv plays out and emits eof
                    self._prebuffer_ready = True
                    self._maybe_release_epoch()
                    return
                if pkt.discontinuity and not first:
                    if trace:
                        print("[trace] discontinuity -> reload", flush=True, file=_sys.stderr)
                    # Seek: fresh container stream -> reload mpv on a new buffer.
                    await self._load_stream()
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
        while True:
            await asyncio.sleep(0.5)
            # Each property read fails independently: a transient error on one
            # must not skip the buffered_ms update — the server paces on the
            # reported value, and a frozen stale report wedges its
            # backpressure pause while the real buffer drains.
            def _prop(name, default=None):
                try:
                    return getattr(self.mpv, name)
                except Exception:
                    return default

            pos = _prop("time_pos")
            avsync = _prop("avsync")
            drop = _prop("frame_drop_count")
            cache = _prop("demuxer_cache_duration")
            buffering = bool(_prop("paused_for_cache"))
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
            if buffering != was_buffering:
                was_buffering = buffering
                self.rebuffering.emit(buffering)
            if pos is not None:
                hwdec = _prop("hwdec_current") or "sw"
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
            current_uri = self._buffer.uri if self._buffer is not None else None
            tracks_belong_to_current_epoch = (
                current_uri is not None
                and not self._reloading
                and _prop("path") == current_uri
                and (self._source_path is None or self._external_ready)
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
