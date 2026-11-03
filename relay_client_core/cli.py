"""Mock client CLI: plays a file through the relay server.

    python -m relay_client_core.cli sample.mkv --model passthrough
    python -m relay_client_core.cli sample.mkv --mpv          # pipe video to mpv
    python -m relay_client_core.cli sample.mkv --decode       # decode + verify PTS

Interactive keys (Windows console): space = play/pause, ← / → = seek ∓/± 10 s,
q = quit.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import asyncio
import sys
import tempfile
import time
from fractions import Fraction

import av

from relay_protocol import NO_TS
from relay_protocol.video import RESIZE_ALGORITHMS

from .client import DisplayTarget, RelayClient, SessionConfig


class PlaybackSim:
    """Simulated presentation clock: consumes downlink packets, computes
    buffered_ms for buffer reports the way a real player's buffer would."""

    def __init__(self, time_base: Fraction):
        self.time_base = time_base
        self.received_packets = 0
        self.newest_pts_s = 0.0
        self.start_wall: float | None = None
        self.start_pts_s: float | None = None
        self.playing = False
        self.eos = False

    def on_packet(self, pts: int) -> None:
        if pts == NO_TS:
            return
        self.received_packets += 1
        self.newest_pts_s = max(self.newest_pts_s, float(pts * self.time_base))
        if self.start_pts_s is None:
            self.start_pts_s = float(pts * self.time_base)

    def play(self) -> None:
        if not self.playing:
            self.playing = True
            base = self.position_s if self.start_wall is not None else (self.start_pts_s or 0.0)
            self.start_pts_s = base
            self.start_wall = time.monotonic()

    def pause(self) -> None:
        if self.playing:
            self.start_pts_s = self.position_s
            self.playing = False
        self.start_wall = time.monotonic()

    def seek_to(self, pts_s: float) -> None:
        self.newest_pts_s = pts_s
        self.start_pts_s = pts_s
        self.start_wall = time.monotonic()

    @property
    def position_s(self) -> float:
        if self.start_pts_s is None:
            return 0.0
        if not self.playing or self.start_wall is None:
            return self.start_pts_s
        return self.start_pts_s + (time.monotonic() - self.start_wall)

    @property
    def buffered_ms(self) -> int:
        return max(0, int((self.newest_pts_s - self.position_s) * 1000))


async def run(args) -> int:
    client = RelayClient(args.host, args.port)
    try:
        with ExitStack() as resources:
            return await _run_session(args, client, resources)
    finally:
        await client.teardown()


async def _run_session(args, client, resources: ExitStack) -> int:
    caps = await client.connect()
    print(f"server: {caps['server_name']} | models: {[m['name'] for m in caps['models']]}")

    dw, _, dh = args.display.partition("x")
    session = await client.open_session(SessionConfig(
        path=args.file, model=args.model, quality_tier=args.tier,
        display_w=int(dw), display_h=int(dh), fit_mode=args.fit_mode,
        resize_algorithm=None if args.resize_algorithm == "server-default" else args.resize_algorithm,
        high_bit_depth=args.high_bit_depth, display_target=args.display_target,
    ))
    print(f"session {session.session_id}: downlink {session.downlink_codec} "
          f"{session.downlink_width}x{session.downlink_height} {session.downlink_bit_depth}-bit"
          f"{' HDR' if session.downlink_hdr else ''}"
          f"{' + Dolby Vision RPU' if session.downlink_dolby_vision else ''}")
    if args.display_target is not None:
        print(f"display mapping: {session.downlink_mapping}"
              f"{'' if session.display_target else ' (not negotiated)'}")

    await client.attach_media()
    sim = PlaybackSim(client.track.time_base)

    # Downlink is a self-describing container stream (docs/PROTOCOL.md §3.2);
    # Optional verification spools to disk: normal playback retains no payload,
    # and even a multi-hour --decode run has bounded Python memory.
    epoch_file = resources.enter_context(tempfile.TemporaryFile()) if args.decode else None

    mpv_proc = None
    if args.mpv:
        mpv_proc = await asyncio.create_subprocess_exec(
            "mpv", "--no-terminal", "-",
            stdin=asyncio.subprocess.PIPE,
        )

    out_file = resources.enter_context(open(args.save, "wb")) if args.save else None

    await client.start_uplink()
    await client.play()
    sim.play()

    async def consume() -> None:
        q = client.downlink_queue()
        while True:
            pkt = await q.get()
            if pkt is None:
                break
            if pkt.epoch < client.epoch:
                continue
            if pkt.eos:
                sim.eos = True
                break
            sim.on_packet(pkt.pts)
            client.buffered_ms = sim.buffered_ms
            if epoch_file is not None:
                if pkt.discontinuity:
                    epoch_file.seek(0)
                    epoch_file.truncate()
                epoch_file.write(pkt.payload)
            if mpv_proc is not None:
                mpv_proc.stdin.write(pkt.payload)
                await mpv_proc.stdin.drain()
            if out_file is not None:
                out_file.write(pkt.payload)

    consume_task = asyncio.create_task(consume())

    async def report_live() -> None:
        # Keep buffered_ms current even while the server is paused on the
        # watermark — updating it only per received packet deadlocked the
        # pause/resume cycle (server waits for the buffer to drain, client
        # never re-reports).
        while not consume_task.done():
            client.buffered_ms = sim.buffered_ms
            await asyncio.sleep(0.25)

    report_live_task = asyncio.create_task(report_live())

    async def keyboard() -> None:
        if sys.platform != "win32" or not sys.stdin.isatty():
            await asyncio.Event().wait()
        import msvcrt

        paused = False
        while True:
            await asyncio.sleep(0.05)
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == " ":
                    paused = not paused
                    if paused:
                        await client.pause()
                        sim.pause()
                    else:
                        await client.play()
                        sim.play()
                    print("paused" if paused else "playing")
                elif ch == "q":
                    consume_task.cancel()
                    return
                elif ch == "\xe0":  # arrow key prefix
                    arrow = msvcrt.getwch()
                    delta = {"K": -10.0, "M": 10.0}.get(arrow)
                    if delta is not None:
                        target_s = max(0.0, sim.position_s + delta)
                        tb = client.track.time_base
                        target_pts = int(target_s / tb)
                        print(f"seek -> {target_s:.1f}s")
                        await client.seek(target_pts)
                        sim.seek_to(target_s)

    kb_task = asyncio.create_task(keyboard())
    status_interval = 2.0
    try:
        while not consume_task.done():
            await asyncio.wait([consume_task], timeout=status_interval)
            if not consume_task.done():
                print(f"pos {sim.position_s:6.1f}s | buffered {sim.buffered_ms:5d} ms | "
                      f"pkts {sim.received_packets}", file=sys.stderr)
        if not consume_task.cancelled():
            await consume_task  # propagate read/write failures, rather than report success
    finally:
        for task in (kb_task, report_live_task, consume_task):
            task.cancel()
        await asyncio.gather(kb_task, report_live_task, consume_task, return_exceptions=True)
        if mpv_proc is not None:
            mpv_proc.stdin.close()
            await mpv_proc.wait()

    print(f"downlink packets: {sim.received_packets}, eos: {sim.eos}")
    source_error = getattr(client, "source_error", None)
    if source_error:  # the client ended the downlink: reading the file failed
        print(f"relay-client: {source_error}", file=sys.stderr)
        return 1
    if epoch_file is not None and epoch_file.tell():
        epoch_file.seek(0)
        decoded_frames = 0
        with av.open(epoch_file) as container:
            for _ in container.decode(container.streams.video[0]):
                decoded_frames += 1
        print(f"decoded frames: {decoded_frames}")
    return 0


def _display_target(text: str) -> DisplayTarget:
    """pq:PEAK[:WHITE] or sdr[:WHITE], luminances in nits."""
    transfer, *values = text.split(":")
    try:
        numbers = [float(value) for value in values]
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a luminance in {text!r}") from None
    if transfer == "pq" and len(numbers) in (1, 2):
        return DisplayTarget("pq", *numbers)
    if transfer == "sdr" and len(numbers) <= 1:
        return DisplayTarget("sdr", None, *numbers)
    raise argparse.ArgumentTypeError("expected pq:PEAK[:WHITE] or sdr[:WHITE]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="relay-client")
    parser.add_argument("file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8590)
    parser.add_argument("--model", default="passthrough")
    parser.add_argument("--tier", default="lossless-hevc")
    parser.add_argument("--display", default="1920x1080")
    parser.add_argument("--fit-mode", choices=["fit", "cover"], default="fit",
                        help="fit inside the display (letterbox) or cover it (crop)")
    parser.add_argument(
        "--resize-algorithm",
        choices=["server-default", *RESIZE_ALGORITHMS],
        default="server-default",
        help="server-side post-ONNX downscale filter",
    )
    parser.add_argument(
        "--high-bit-depth", choices=["hdr", "all"], default=None,
        help="ask for a 10-bit downlink: HDR sources, or every source",
    )
    parser.add_argument(
        "--display-target", type=_display_target, default=None, metavar="pq:PEAK[:WHITE] | sdr[:WHITE]",
        help="the display to map Dolby Vision and HDR10+ for (PROTOCOL §3.6); needs --high-bit-depth",
    )
    parser.add_argument("--decode", action="store_true", help="decode downlink and count frames")
    parser.add_argument("--mpv", action="store_true", help="pipe downlink video into mpv")
    parser.add_argument("--save", help="write raw downlink bitstream to file")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
