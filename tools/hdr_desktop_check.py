"""Check HDR output in the real desktop window, and time it.

    python tools/hdr_desktop_check.py --file hdr.mkv [--server HOST:PORT] \
        [--shots DIR --shot-cmd 'grim {path}'] [--surface auto|force|off] [--pacing]

Plays an HDR clip (``upscale-cli sample hdr.mkv --hdr``) on its own, or
through a relay with --server (uplink, passthrough model by default), in a
visible window. Prints the display's HDR state, where the video renders and
what mpv outputs, and takes a screenshot as each overlay appears (--shots).
--pacing times fullscreen transitions, sidebar slides, a live splitter drag,
overlay motion and steady playback: the rates CLAUDE.md quotes for the
widget, here with the video on the HDR surface. --surface off measures the
same on the widget, for comparison (RELAY_HDR_SURFACE).
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import locale
import os
import shlex
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QObject, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402


class Clock(QObject):
    """Timestamps of the events a measurement compares."""

    def __init__(self, window):
        super().__init__()
        self.paints: list[tuple[float, float]] = []  # (start, duration) of presenter paints
        self.resizes: list[float] = []  # the player widget's
        self.swaps: list[float] = []  # main window frames (the player's frameSwapped)
        self.steps: list[float] = []  # overlay animation steps
        window.player.installEventFilter(self)
        window.player.frameSwapped.connect(lambda: self.swaps.append(time.perf_counter()))
        for reveal in (window._controls_reveal, window._settings_reveal, window._track_reveal):
            reveal._animation.valueChanged.connect(lambda _v: self.steps.append(time.perf_counter()))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.resizes.append(time.perf_counter())
        return False

    def window(self, name, t0, t1, anchor="resizes"):
        """Rates over the span the anchor events cover within [t0, t1]."""
        anchors = [t for t in getattr(self, anchor) if t0 <= t <= t1]
        if len(anchors) < 2:
            return {"name": name, "anchor": anchor, "events": len(anchors)}
        a, b = anchors[0], anchors[-1]
        span = b - a
        paints = [p for p in self.paints if a <= p[0] <= b + 0.02]
        durations = sorted(p[1] * 1000 for p in paints)

        def median(values):
            values = sorted(values)
            return round(values[len(values) // 2], 2) if values else None

        phases = [p[2] for p in paints if len(p) > 2 and p[2]]
        return {
            "name": name,
            "span_ms": round(span * 1000),
            f"{anchor}_per_s": round(len(anchors) / span, 1),
            "main_frames_per_s": round(len([t for t in self.swaps if a <= t <= b + 0.02]) / span, 1),
            "surface_paints_per_s": round(len(paints) / span, 1),
            "surface_paint_ms_median": median(durations),
            "surface_paint_ms_max": round(durations[-1], 2) if durations else None,
            # Where the surface's paints went (HdrPresenter.last_phases):
            # mpv renders, and the medians of each phase.
            "mpv_renders": sum(1 for p in phases if p.get("rendered")),
            "video_ms_median": median(p["video_ms"] for p in phases),
            "video_ms_median_rendering": median(p["video_ms"] for p in phases if p.get("rendered")),
            "mirror_ms_median": median(p["mirror_ms"] for p in phases),
            "overlays_ms_median": median(p["overlays_ms"] for p in phases),
        }


async def wait_until(predicate, description, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise AssertionError(f"timed out waiting for {description}") from error


async def shot(args, name, window=None):
    if not args.shots:
        return
    path = Path(args.shots) / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.shot_cmd == "qt":  # the screen under the window, as Qt grabs it (Windows)
        screen = window.screen()
        g = window.geometry()
        screen.grabWindow(0, g.x() - screen.geometry().x(), g.y() - screen.geometry().y(),
                          g.width(), g.height()).save(str(path))
        return
    process = await asyncio.create_subprocess_shell(args.shot_cmd.format(path=shlex.quote(str(path))))
    await process.wait()


def surface_state(window):
    player = window.player
    presenter = player._presenter
    state = {"hdr_output": player.hdr_output, "on_surface": player._on_presenter}
    if presenter is not None:
        described = getattr(presenter, "_described", None)  # (surface, peak, white) on Wayland
        state.update(pq=presenter.pq, sdr_white_nits=presenter.sdr_white_nits,
                     peak_nits=getattr(presenter, "peak_nits", None),
                     described={"peak": described[1], "white": described[2]} if described else None,
                     color_space=presenter.format().colorSpace().description(),
                     depth=[presenter.format().redBufferSize(), presenter.format().alphaBufferSize()],
                     broken=presenter.broken)
    mpv = player.mpv
    for name in ("target-trc", "target-prim", "target-peak", "current-vo", "hwdec-current"):
        try:
            state[name] = mpv._get_property(name)
        except Exception as error:  # noqa: BLE001
            state[name] = repr(error)
    for name in ("video-params",):  # video-target-params stays empty under the render API
        try:
            params = mpv._get_property(name) or {}
            state[name] = {k: params.get(k) for k in ("gamma", "primaries", "sig-peak", "max-luma", "w", "h", "pixelformat")}
        except Exception as error:  # noqa: BLE001
            state[name] = repr(error)
    return state


async def measure(window, clock, report):
    player = window.player
    results = report.setdefault("pacing", [])

    async def span(name, action, seconds, anchor="resizes"):
        t0 = time.perf_counter()
        action()
        await asyncio.sleep(seconds)
        results.append(clock.window(name, t0, time.perf_counter(), anchor))
        print(json.dumps(results[-1]), flush=True)

    for i in range(2):
        await span(f"fullscreen enter {i}", window.toggle_fullscreen, 2.0)
        await span(f"fullscreen leave {i}", window.toggle_fullscreen, 2.0)
    await span("sidebar hide", window.browser_toggle.click, 1.0)
    await span("sidebar show", window.browser_toggle.click, 1.0)

    # A live splitter drag: the handle swings 60 px either way of where it
    # was, a few pixels each main frame. It stops itself: each move brings
    # the next frame, and on Windows a queue that never empties starves the
    # timers asyncio.sleep relies on.
    sizes = window.split.sizes()
    step = {"n": 0, "moved": False}
    t0 = time.perf_counter()

    def drag():
        step["n"] += 1
        if time.perf_counter() - t0 > 1.5:
            window.player.frameSwapped.disconnect(drag)
            window.split.setSizes(sizes)
            return
        phase = step["n"] % 40
        offset = 6 * (phase if phase < 10 else 20 - phase if phase < 30 else phase - 40)
        window.split.setSizes([sizes[0] + offset, sizes[1] - offset])
        step["moved"] |= window.split.sizes() != sizes

    window.player.frameSwapped.connect(drag)
    drag()
    await asyncio.sleep(2.0)
    if step["moved"]:
        results.append(clock.window("splitter drag", t0, t0 + 1.5))
    else:
        # A tiled window narrower than the sidebar's and the player's minimum
        # widths together leaves the handle nowhere to go: float it, or give
        # it a wider tile.
        results.append({"name": "splitter drag", "skipped": f"no room to drag at sizes {sizes}"})
    print(json.dumps(results[-1]), flush=True)
    await asyncio.sleep(0.5)

    await span("settings sheet show", lambda: window._set_settings_visible(True), 1.0, "steps")
    await span("settings sheet hide", lambda: window._set_settings_visible(False), 1.0, "steps")
    await span("track card show", lambda: window._set_track_panel_visible(True), 1.0, "steps")
    await span("track card hide", lambda: window._set_track_panel_visible(False), 1.0, "steps")
    window.toggle_fullscreen()
    await asyncio.sleep(2.0)
    bottom = player.height() - 4
    await span("fullscreen bar show", lambda: window._on_player_mouse_moved(player.width() // 2, bottom), 1.0, "steps")
    await span("fullscreen bar hide", window._auto_hide_controls, 1.0, "steps")
    window.toggle_fullscreen()
    await asyncio.sleep(2.0)

    drops = player._observation("frame-drop-count", 0) or 0
    n0, t0 = len(clock.paints), time.perf_counter()
    await asyncio.sleep(5.0)
    results.append({
        "name": "steady playback",
        "surface_paints_per_s": round((len(clock.paints) - n0) / (time.perf_counter() - t0), 1),
        "mpv_drops": (player._observation("frame-drop-count", 0) or 0) - drops,
    })
    print(json.dumps(results[-1]), flush=True)


async def exercise(window, clock, args):
    from desktop_client import hdr_output  # noqa: PLC0415

    report = {"surface_mode": os.environ.get("RELAY_HDR_SURFACE", "auto")}
    report["display"] = asdict(hdr_output.query(window))
    if hdr_output.platform_kind() == "wayland":
        report["outputs"] = hdr_output.wayland_outputs()
    print(json.dumps(report), flush=True)
    errors = []
    window._error = lambda title, message: errors.append(f"{title}: {message}")
    player = window.player
    try:
        if args.server:
            window.host_edit.setText(args.server)
            await window.on_connect()
            assert window.client is not None, errors
            window.model_combo.setCurrentText(args.model)
            window.tier_combo.setCurrentIndex(max(0, window.tier_combo.findData(args.tier)))
            await window._start_session(args.file, source=args.source)
            assert window.client.session is not None, errors
            report["downlink"] = {
                "codec": window.client.session.downlink_codec,
                "bit_depth": window.client.session.downlink_bit_depth,
                "color": window.client.session.downlink_color,
            }
        else:
            import av  # noqa: PLC0415

            with av.open(args.file) as container:
                transfer = int(container.streams.video[0].codec_context.color_trc)
            report["source_hdr"] = transfer in (16, 18)  # PQ, HLG
            window._session_source = "local"
            window._session_path = args.file
            window.idle_hint.hide()
            await player.play_local(args.file, 0.0, hdr=report["source_hdr"])
            player.mpv["loop-file"] = "inf"  # a short clip would end before the pacing spans
        await wait_until(lambda: (player._observation("time-pos", 0) or 0) > 0.5, "playback")
        await asyncio.sleep(1.0)
        report["surface"] = surface_state(window)
        print(json.dumps({"surface": report["surface"]}), flush=True)
        await shot(args, "01_playing", window)
        window._set_track_panel_visible(True)
        await asyncio.sleep(0.8)
        await shot(args, "02_track_card", window)
        window._set_track_panel_visible(False)
        window._set_settings_visible(True)
        await asyncio.sleep(0.8)
        await shot(args, "03_settings_sheet", window)
        window._set_settings_visible(False)
        await asyncio.sleep(0.5)
        window.toggle_fullscreen()
        await asyncio.sleep(2.0)
        await shot(args, "04_fullscreen", window)
        window._on_player_mouse_moved(player.width() // 2, player.height() - 4)
        await asyncio.sleep(1.0)
        await shot(args, "05_fullscreen_bar", window)
        window.toggle_fullscreen()
        await asyncio.sleep(2.0)
        if args.pacing:
            await measure(window, clock, report)
        report["errors"] = errors
        return report
    finally:
        player.stop()
        if window.client is not None:
            await window.client.close()
            window.client = None
        await asyncio.sleep(0.3)


def main():
    faulthandler.enable(all_threads=sys.platform != "win32")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="an HDR clip (upscale-cli sample --hdr)")
    parser.add_argument("--server", help="relay HOST:PORT; without it the file plays locally")
    parser.add_argument("--source", choices=("uplink", "server_file"), default="uplink")
    parser.add_argument("--model", default="passthrough")
    parser.add_argument("--tier", default="lossless-hevc")
    parser.add_argument("--surface", choices=("auto", "force", "off"), default="auto")
    parser.add_argument("--shots", help="directory for screenshots")
    parser.add_argument("--shot-cmd", default="grim {path}", help="screenshot command ({path} is replaced), or qt for Qt's own grab")
    parser.add_argument("--pacing", action="store_true")
    parser.add_argument("--size", default="1400x860", help="window size before fullscreen")
    args = parser.parse_args()
    if args.surface != "auto":
        os.environ["RELAY_HDR_SURFACE"] = args.surface
    app = QApplication([])
    locale.setlocale(locale.LC_NUMERIC, "C")
    from desktop_client import hdr_view  # noqa: PLC0415
    from desktop_client.main_window import MainWindow  # noqa: PLC0415
    from desktop_client.options import DesktopOptions  # noqa: PLC0415

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    scope = f"hdr-check-{uuid.uuid4().hex}"
    window = MainWindow(DesktopOptions(settings_scope=scope, discovery=False))
    clock = Clock(window)
    def timed(paint):
        def timed_paint(self):
            started = time.perf_counter()
            paint(self)
            phases = dict(getattr(self, "last_phases", None) or {})
            clock.paints.append((started, time.perf_counter() - started, phases))
            if hasattr(self, "last_phases"):
                self.last_phases = {}  # each paint's own, not a stale one
        return timed_paint

    hdr_view.HdrPresenter.paintGL = timed(hdr_view.HdrPresenter.paintGL)
    if sys.platform == "win32":
        from desktop_client import hdr_d3d11  # noqa: PLC0415

        hdr_d3d11.D3D11Presenter._paint = timed(hdr_d3d11.D3D11Presenter._paint)
    width, _, height = args.size.partition("x")
    # On the primary screen, whole: a window across two screens grabs badly.
    window.setGeometry(app.primaryScreen().availableGeometry().x() + 40,
                       app.primaryScreen().availableGeometry().y() + 40, int(width), int(height))
    window.show()
    try:
        with loop:
            result = loop.run_until_complete(exercise(window, clock, args))
        print(json.dumps(result, indent=2, default=str))
    finally:
        window.close()
        settings = QSettings("upscale-relay", scope)
        settings.clear()
        settings.sync()
        if Path(settings.fileName()).is_file():  # Linux: clear() leaves the empty file
            Path(settings.fileName()).unlink()


if __name__ == "__main__":
    main()
