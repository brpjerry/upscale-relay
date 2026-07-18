"""Stream metadata inspection and output verification."""

from __future__ import annotations

import av


def print_info(path: str) -> None:
    with av.open(path) as container:
        duration = container.duration / av.time_base if container.duration else None
        print(f"file:      {path}")
        print(f"format:    {container.format.long_name}")
        print(f"duration:  {duration:.3f}s" if duration is not None else "duration:  unknown")
        print(f"bit rate:  {container.bit_rate}" if container.bit_rate else "bit rate:  unknown")
        for stream in container.streams:
            kind = stream.type
            line = f"  #{stream.index} {kind}: {stream.codec_context.name if stream.codec_context else '?'}"
            if kind == "video":
                cc = stream.codec_context
                line += (
                    f" {cc.width}x{cc.height} {cc.pix_fmt}"
                    f" | avg rate {stream.average_rate}"
                    f" | time_base {stream.time_base}"
                    f" | frames {stream.frames or 'unknown'}"
                )
            elif kind == "audio":
                cc = stream.codec_context
                line += f" {cc.sample_rate} Hz, {cc.layout.name}"
            elif kind == "subtitle":
                lang = stream.metadata.get("language", "und")
                line += f" ({lang})"
            print(line)


def count_video_packets(path: str) -> tuple[int, float | None]:
    """Count packets in the first video stream (== frame count for video) and
    return (count, last_pts_seconds + one frame duration if derivable)."""
    with av.open(path) as container:
        stream = container.streams.video[0]
        count = 0
        max_pts = None
        for packet in container.demux(stream):
            if packet.pts is None:
                continue
            count += 1
            if max_pts is None or packet.pts > max_pts:
                max_pts = packet.pts
        duration = None
        if max_pts is not None:
            duration = float(max_pts * stream.time_base)
            if stream.average_rate:
                duration += 1.0 / float(stream.average_rate)
        return count, duration


def verify_pts(output_path: str, pts_expected: list[float], tolerance: float = 0.002) -> bool:
    """Assert the output's PTS sequence matches what the pipeline wrote.

    Compares sorted presentation times in seconds; tolerance absorbs container
    time_base rounding (e.g. MKV's 1/1000).
    """
    with av.open(output_path) as container:
        stream = container.streams.video[0]
        pts_out = sorted(
            float(p.pts * stream.time_base) for p in container.demux(stream) if p.pts is not None
        )
    expected = sorted(pts_expected)
    if len(pts_out) != len(expected):
        print(f"VERIFY FAIL: {len(expected)} frames written, {len(pts_out)} in output")
        return False
    worst = max((abs(a - b) for a, b in zip(expected, pts_out)), default=0.0)
    if worst > tolerance:
        print(f"VERIFY FAIL: PTS drift up to {worst * 1000:.2f} ms (tolerance {tolerance * 1000:.1f} ms)")
        return False
    print(f"VERIFY OK: PTS sequence matches ({len(pts_out)} frames, max drift {worst * 1000:.2f} ms)")
    return True


def verify_passthrough(input_path: str, output_path: str, frames_decoded: int) -> bool:
    """Check the output has the same frame count and (approximately) the same
    duration as what was decoded from the input."""
    out_count, out_duration = count_video_packets(output_path)
    in_count, in_duration = count_video_packets(input_path)

    ok = True
    if out_count != frames_decoded:
        print(f"VERIFY FAIL: wrote {out_count} frames, decoded {frames_decoded}")
        ok = False
    if in_count != frames_decoded:
        # Informational: some containers carry packets that don't decode to frames.
        print(f"note: input has {in_count} packets, {frames_decoded} decoded frames")
    if in_duration and out_duration:
        if abs(in_duration - out_duration) > 0.05:
            print(f"VERIFY FAIL: duration in={in_duration:.3f}s out={out_duration:.3f}s")
            ok = False
    if ok:
        print(f"VERIFY OK: {out_count} frames, duration {out_duration:.3f}s")
    return ok
