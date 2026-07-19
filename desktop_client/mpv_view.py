"""libmpv-backed player view.

The downlink is a streaming Matroska byte stream with original PTS
(docs/PROTOCOL.md §3.2), fed to mpv through a localhost TCP stream. The
original file is attached via --external-files, so its audio (master clock)
and subtitle tracks play alongside the network video, all aligned by real
timestamps — including after seeks, where a fresh container starts at the
seek target and mpv reloads it.

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
    rebuffering = Signal(bool)
    seek_requested = Signal(float)  # relative seconds (arrow keys)
    chapter_step_requested = Signal(int)  # +1 next / -1 previous (PgUp/PgDn)
    finished = Signal()
    failed = Signal(str)
    fullscreen_toggled = Signal()  # F key / double-click; the window fullscreens
    mouse_moved = Signal(int, int)  # cursor x, y in the view; drives fullscreen control reveal
    _frame_ready = Signal()  # mpv render thread -> queued repaint on GUI thread

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

    PREBUFFER_S = 1.5

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
            extra = {"vo": "null", "ao": "null", "osc": "no"}
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
                "demuxer_readahead_secs": "15",
                "demuxer_max_bytes": "768MiB",
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
            self.mpv.demuxer_readahead_secs = 15  # ~200 Mbps lossless tiers
            self.mpv.demuxer_max_bytes = "768MiB"
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
        self._reloading = False
        self.client = None

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
        self._task = asyncio.create_task(self._consume(downlink_q))
        self._stats_task = asyncio.create_task(self._stats_loop())

    async def _load_stream(self) -> None:
        """(Re)start mpv on a fresh buffer — session start and every seek.

        On reload the old file is stopped first, then we YIELD to the event
        loop before loading the new one. Doing stop+load back-to-back
        synchronously (or polling mpv properties in between) raced mpv's event
        thread and crashed (native AV) intermittently on cold start.
        """
        if self._buffer is not None:
            self._reloading = True
            self._buffer.close()  # old catchall reader returns -> EOF
            try:
                self.mpv.command("stop")
            except Exception:
                pass
            # Let mpv's own threads process the stop/EOF before we touch it
            # again. No synchronous property reads here.
            await asyncio.sleep(0.15)
        self._buffer = _LoopbackStream()
        self._fed = 0
        self.mpv.pause = True
        options = {}
        if self._source_path:
            # Audio (master clock) + subtitle tracks come from the original.
            # audio-file/sub-files mark the tracks for auto-selection;
            # plain external-files tracks are NOT auto-selected by mpv.
            options["audio-file"] = self._source_path
            options["sub-files"] = self._source_path
        # No `start=` option: with rebase-start-time=no the stream's own
        # timestamps place playback at the seek target, and the external
        # audio aligns by absolute time. (A start-seek on the not-yet-cached
        # live stream wedged mpv permanently.)
        self._pending_start = None
        self.mpv.loadfile(self._buffer.uri, **options)
        self._reloading = False

    def stop(self) -> None:
        for task in (self._task, self._stats_task):
            if task is not None:
                task.cancel()
        self._task = self._stats_task = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        try:
            self.mpv.command("stop")
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
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
        """Arm the next stream reload to start at the seek target."""
        self._pending_start = target_s

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
        if self.mpv.sid in (None, False, "no"):
            subs = [t for t in tracks if t.get("type") == "sub"]
            if subs:
                pick = next((t for t in subs if t.get("default")), subs[0])
                self.mpv.sid = pick["id"]
        if self.mpv.aid in (None, False, "no"):
            audio = [t for t in tracks if t.get("type") == "audio"]
            if audio:
                self.mpv.aid = audio[0]["id"]

    def select_subtitle(self, sid: int | None) -> None:
        self.mpv.sid = sid if sid is not None else "no"

    def set_sub_delay(self, seconds: float) -> None:
        self.mpv.sub_delay = seconds

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
            prebuffer_packets = int(self.PREBUFFER_S * self._fps)
            unpaused = False
            first = True
            while True:
                pkt = await q.get()
                if pkt is None:
                    self.failed.emit("downlink closed")
                    return
                if pkt.eos:
                    if self._buffer is not None:
                        self._buffer.finish()  # mpv plays out and emits eof
                    if not unpaused:
                        self.mpv.pause = False
                    return
                if pkt.discontinuity and not first:
                    if trace:
                        print("[trace] discontinuity -> reload", flush=True, file=_sys.stderr)
                    # Seek: fresh container stream -> reload mpv on a new buffer.
                    await self._load_stream()
                    if trace:
                        print("[trace] reload done", flush=True, file=_sys.stderr)
                    unpaused = False
                first = False
                if pkt.payload and self._buffer is not None:
                    self._buffer.feed(pkt.payload)
                    self._fed += 1
                    if trace and self._fed % 48 == 0:
                        print(f"[trace] fed={self._fed} unpaused={unpaused}", flush=True, file=_sys.stderr)
                if not unpaused and self._fed >= prebuffer_packets:
                    if trace:
                        print(f"[trace] unpausing after {self._fed}", flush=True, file=_sys.stderr)
                    self.mpv.pause = False
                    unpaused = True
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
            if not self._tracks_reported:
                tracks = self.mpv.track_list or []
                subs = [(t.get("id"), t.get("title") or t.get("lang") or f"track {t.get('id')}")
                        for t in tracks if t.get("type") == "sub"]
                if tracks:
                    self._tracks_reported = True
                    self._autoselect_tracks(tracks)
                    sid = self.mpv.sid
                    self.track_list_changed.emit(
                        subs, sid if isinstance(sid, int) else None)
