"""Stress the desktop player's decode and render path in the real window.

    RELAY_HWDEC=vaapi python tools/hwdec_stress.py --server HOST:PORT \
        --file 'library/path.mkv' [--minutes 10] [--seed 1]

Plays a relay session (server library file by default, passthrough model) in a
visible window and keeps changing it: seeks far and near, each a stream
reload, pauses, fullscreen in and out, sidebar slides, and whole-session
restarts. It prints each action as it starts, so a crash's last line names
what it interrupted, and a summary at the end. --steady plays without
actions instead (windowed, or --fullscreen), for power measurements.
--local FILE --eof plays the file's last seconds to its natural end, over and
over (mpv's end-of-file teardown, which no freeze precedes).
--local FILE plays a file without the relay instead, where "restart" loads
it again at the current position (the local load path's own stop and load). The hardware decode mode comes
from RELAY_HWDEC (mpv's hwdec names; unset is the player's own default):
this is the tool for deciding whether zero-copy VA-API is safe in the
embedded render path (docs/CLIENT_LINUX.md).
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import locale
import os
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402


def say(**fields) -> None:
    print(json.dumps({"t": round(time.monotonic(), 2), **fields}), flush=True)


async def wait_until(predicate, description, timeout=30, state=None):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            if state is not None:
                say(action="timeout", state=state())
            raise TimeoutError(f"no {description} within {timeout} s")
        await asyncio.sleep(0.1)


def player_state(window):
    player = window.player
    names = ("_frozen", "_thaw_entry", "_started_entry", "_load_generation", "_reloading", "_on_presenter",
             "_epoch_released", "_local_playback", "_caller_paused", "_mpv_paused")
    state = {name: repr(getattr(player, name, None)) for name in names}
    state["observed"] = {k: repr(v) for k, v in player._observed.items()
                         if k in ("time-pos", "pause", "idle-active", "hwdec-current", "core-idle", "eof-reached")}
    state["window_paused"] = getattr(window, "_paused", None)
    return state


async def exercise(window, args):
    errors: list[str] = []
    window._error = lambda title, message: errors.append(f"{title}: {message}")
    player = window.player
    rng = random.Random(args.seed)
    counts: dict[str, int] = {}
    hwdec_seen: set[str] = set()
    if args.local:
        async def start(position=0.0):
            window._session_source = "local"
            window._session_path = args.local
            window.idle_hint.hide()
            await player.play_local(args.local, position, hdr=False)
            player.mpv["loop-file"] = "no" if args.eof else "inf"
            await wait_until(lambda: (player._observation("time-pos", 0) or 0) > 0.2, "playback", 60,
                             lambda: player_state(window))
            window._duration_s = player._observation("duration") or window._duration_s
    else:
        window.host_edit.setText(args.server)
        await window.on_connect()
        assert window.client is not None, errors
        window.model_combo.setCurrentText(args.model)
        window.tier_combo.setCurrentIndex(max(0, window.tier_combo.findData(args.tier)))
        window.settings.ten_bit_video = args.ten_bit

        async def start(position=0.0):
            await window._start_session(args.file, source=args.source)
            assert window.client.session is not None, errors
            await wait_until(lambda: (player._observation("time-pos", 0) or 0) > 0.2, "playback", 60)

    await start()
    duration = window._duration_s or 600.0
    params = player._observation("video-params") or {}
    say(action="playing", duration=duration, hwdec_mode=os.environ.get("RELAY_HWDEC", "(default)"),
        video=f"{params.get('w')}x{params.get('h')} {params.get('hw-pixelformat') or params.get('pixelformat')}")
    deadline = time.monotonic() + args.minutes * 60
    fullscreen = False
    if args.steady:
        if args.fullscreen:
            window.toggle_fullscreen()
            fullscreen = True
        while time.monotonic() < deadline and window.isVisible():
            await asyncio.sleep(1.0)
            hwdec = player._observation("hwdec-current")
            if hwdec:
                hwdec_seen.add(str(hwdec))
        deadline = 0  # skip the actions below
    if args.eof:  # play the file's last seconds to its natural end, again and again
        ended = asyncio.Event()
        player.finished.connect(ended.set)

        def note_hwdec():  # while the file plays: its end idles the decoder
            hwdec = player._observation("hwdec-current")
            if hwdec:
                hwdec_seen.add(str(hwdec))
        while time.monotonic() < deadline and window.isVisible():
            ended.clear()
            counts["eof"] = counts.get("eof", 0) + 1
            say(action="eof", n=counts["eof"], hwdec=player._observation("hwdec-current"))
            await start(max(0.0, duration - rng.uniform(0.6, 2.0)))
            if rng.random() < 0.3:  # resize the video as the file ends
                window._apply_browser_visible(not window.browser_container.isVisible())
            await wait_until(lambda: note_hwdec() or ended.is_set(), "end of file", 30,
                             lambda: player_state(window))
            await asyncio.sleep(rng.uniform(0.0, 1.0))
        deadline = 0
    while time.monotonic() < deadline and window.isVisible():
        roll = rng.random()
        position = player._observation("time-pos", 0) or 0
        if roll < 0.45:
            action, target = "seek_far", rng.uniform(0, max(1.0, duration - 30))
        elif roll < 0.65:
            action, target = "seek_near", max(0.0, position + rng.uniform(-8, 8))
        elif roll < 0.72:
            action, target = "pause_toggle", None
        elif roll < 0.84:
            action, target = "fullscreen", None
        elif roll < 0.92:
            action, target = "sidebar", None
        else:
            action, target = "restart", None
        counts[action] = counts.get(action, 0) + 1
        say(action=action, target=None if target is None else round(target, 1),
            hwdec=player._observation("hwdec-current"))
        if action.startswith("seek"):
            await window._seek_to_seconds(target, announce=False)
        elif action == "pause_toggle":
            await window.on_play_pause()
        elif action == "fullscreen":
            window.toggle_fullscreen()
            fullscreen = not fullscreen
        elif action == "sidebar":
            window._apply_browser_visible(not window.browser_container.isVisible()
                                          if hasattr(window, "browser_container") else rng.random() < 0.5)
        elif args.local:
            await start(position)  # play_local stops the file and loads it again
        else:
            await window._teardown_session()
            await asyncio.sleep(rng.uniform(0.1, 0.6))
            await start()
        hwdec = player._observation("hwdec-current")
        if hwdec:
            hwdec_seen.add(str(hwdec))
        await asyncio.sleep(rng.uniform(0.3, 2.5))
    if fullscreen:
        window.toggle_fullscreen()
    return {"counts": counts, "hwdec_seen": sorted(hwdec_seen), "errors": errors,
            "frame_drops": player._observation("frame-drop-count")}


def main():
    faulthandler.enable(all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", help="relay HOST:PORT")
    parser.add_argument("--file", help="a server library path (or a local file with --source uplink)")
    parser.add_argument("--local", help="play this file without the relay instead")
    parser.add_argument("--source", choices=("server_file", "uplink"), default="server_file")
    parser.add_argument("--model", default="passthrough")
    parser.add_argument("--tier", default="hevc-qp4")
    parser.add_argument("--ten-bit", action="store_true", help="relay sessions: Use 10 bit video")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steady", action="store_true", help="play without actions (power measurements)")
    parser.add_argument("--fullscreen", action="store_true", help="with --steady: play fullscreen")
    parser.add_argument("--eof", action="store_true",
                        help="with --local: play the file's end again and again (mpv's end-of-file teardown)")
    parser.add_argument("--size", default="1400x860", help="window size before fullscreen")
    args = parser.parse_args()
    if not args.local and not (args.server and args.file):
        parser.error("--server and --file, or --local")
    if args.eof and not args.local:
        parser.error("--eof needs --local")
    app = QApplication([])
    locale.setlocale(locale.LC_NUMERIC, "C")
    from desktop_client.main_window import MainWindow  # noqa: PLC0415
    from desktop_client.options import DesktopOptions  # noqa: PLC0415

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    scope = f"hwdec-stress-{uuid.uuid4().hex}"
    window = MainWindow(DesktopOptions(settings_scope=scope, discovery=False))
    width, _, height = args.size.partition("x")
    geometry = app.primaryScreen().availableGeometry()
    window.setGeometry(geometry.x() + 40, geometry.y() + 40, int(width), int(height))
    window.show()
    try:
        with loop:
            result = loop.run_until_complete(exercise(window, args))
        say(action="done", **result)
    finally:
        window.player.stop()
        window.close()
        # Shut mpv down before Python does: its callbacks, still firing after
        # the stop, crashed the interpreter's exit (SIGSEGV in 2 of 8 runs).
        window.player._free_render_ctx()
        window.player.mpv.terminate()
        settings = QSettings("upscale-relay", scope)
        settings.clear()
        settings.sync()
        if Path(settings.fileName()).is_file():  # Linux: clear() leaves the empty file
            Path(settings.fileName()).unlink()


if __name__ == "__main__":
    main()
