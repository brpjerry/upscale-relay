"""Exercise the actual desktop controller/libmpv against a running relay.

    QT_QPA_PLATFORM=offscreen python tools/smoke_desktop.py \
        --server 192.0.2.10:8590 --file /path/to/a/long-test-clip.mkv

Use a clip of at least 30 seconds. The default passthrough model isolates
transport/playback from GPU contention; add --model only after that passes.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import locale
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from desktop_client.main_window import MainWindow
from desktop_client.options import DesktopOptions


async def wait_until(predicate, description, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise AssertionError(f"timed out waiting for {description}") from error


async def exercise(window, args):
    errors = []
    def record_error(title, message):
        errors.append(f"{title}: {message}")
        print(errors[-1], file=sys.stderr, flush=True)
    window._error = record_error
    completed = []
    try:
        window.host_edit.setText(args.server)
        await window.on_connect()
        assert window.client is not None, errors
        if args.external_aux:
            # Exercise the supported fallback path even when this server can
            # mux the particular test fixture's auxiliary codecs.
            window._server_caps["muxed_aux_tracks"] = False
        window.model_combo.setCurrentText(args.model)
        tier = window.tier_combo.findData(args.tier)
        assert tier >= 0, f"server does not advertise {args.tier}"
        window.tier_combo.setCurrentIndex(tier)
        await window._start_session(args.file, source=args.source)
        assert window.client is not None and window.client.session is not None, errors
        await wait_until(lambda: window._position_s > 0.5, "initial playback")
        assert window._duration_s is not None and window._duration_s >= 30, "use a clip >=30 seconds"
        completed.append("playback")

        # Exercise the actual keyboard signal/controller, not just mpv.pause.
        window.player.keyPressEvent(QKeyEvent(
            QKeyEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " "))
        await wait_until(lambda: window._paused and window.player.mpv.pause, "keyboard pause")
        await asyncio.sleep(0.6)
        position = window._position_s
        await asyncio.sleep(0.6)
        assert abs(window._position_s - position) < 0.2, "paused playback advanced"
        completed.append("keyboard pause")

        await window._seek_to_seconds(12.0)
        await wait_until(lambda: abs(window._position_s - 12.0) < 0.5
                         and window.player._epoch_released, "paused seek")
        assert window._paused and window.player.mpv.pause, "seek lost pause intent"
        await wait_until(lambda: abs((window.player.mpv.time_pos or 0) - 12.0) < 0.5,
                         "native paused seek position")
        if args.expect_subtitle:
            await wait_until(lambda: args.expect_subtitle in (window.player.mpv.sub_text or ""),
                             "subtitle spanning the seek target")
            completed.append("subtitle overlap")
        completed.append("paused seek")

        await asyncio.gather(*(window._seek_to_seconds(target) for target in (4.0, 16.0, 8.0)))
        await wait_until(lambda: abs(window._position_s - 8.0) < 0.5
                         and window.player._epoch_released, "overlapping seeks")
        assert window.player.mpv.pause, "seek storm resumed paused playback"
        await wait_until(lambda: abs((window.player.mpv.time_pos or 0) - 8.0) < 0.5,
                         "native overlapping seek position")
        completed.append("overlapping seeks")
        await window.on_play_pause()
        await wait_until(lambda: window._position_s > 8.5, "resume after seeking")
        completed.append("resume")

        tracks = window.player.mpv.track_list or []
        if args.source == "uplink":
            await window.on_fallback()
            assert window._session_source == "local", errors
            await window.on_play_pause()
            await wait_until(lambda: window.player.mpv.pause, "local pause")
            await window._seek_to_seconds(5.0)
            await wait_until(lambda: abs(window._position_s - 5.0) < 0.5, "local seek")
            assert window.player.mpv.pause
            await window.on_play_pause()
            await wait_until(lambda: window._position_s > 5.5, "local resume")
            completed.append("local fallback transport")
        await window.on_stop()
        assert window._session_source is None
        assert not window.stop_btn.isEnabled()
        completed.append("stop")
        assert not errors, errors
        return {"passed": completed, "model": args.model, "tier": args.tier,
                "tracks": [{key: track.get(key) for key in ("type", "codec", "lang")}
                           for track in tracks]}
    finally:
        window.player.stop()
        if window.client is not None:
            await window.client.close()
            window.client = None
        await asyncio.sleep(0)


def main():
    faulthandler.enable()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--file", required=True, help="local file or server-library-relative path")
    parser.add_argument("--source", choices=("uplink", "server_file"), default="uplink")
    parser.add_argument("--model", default="passthrough")
    parser.add_argument("--tier", default="lossless-hevc")
    parser.add_argument("--expect-subtitle", help="subtitle text expected at the 12-second seek")
    parser.add_argument("--external-aux", action="store_true",
                        help="exercise server-file external audio/subtitle attachment")
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.external_aux and args.source != "server_file":
        parser.error("--external-aux requires --source server_file")
    app = QApplication([])
    locale.setlocale(locale.LC_NUMERIC, "C")
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    scope = f"smoke-{uuid.uuid4().hex}"
    window = MainWindow(DesktopOptions(headless=True, trace=args.trace, settings_scope=scope))
    try:
        with loop:
            result = loop.run_until_complete(exercise(window, args))
        print(json.dumps(result, indent=2))
    finally:
        window.close()
        window.player.mpv.terminate()
        settings = QSettings("upscale-relay", scope)
        settings.clear()
        settings.sync()


if __name__ == "__main__":
    main()
