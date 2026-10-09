"""Real decode -> encode -> mux runs of the server pipeline on generated media.

Each test writes a small source file, feeds its packets through a CPU
passthrough Pipeline exactly as the session does, and decodes the downlink
Matroska stream it produced.
"""

from __future__ import annotations

import base64
import io
import threading
import time
from fractions import Fraction

import av
import numpy as np
import pytest

from relay_media import VideoTrack
from relay_protocol import FLAG_EOS, MediaPacket
from relay_server.pipeline import Pipeline, VideoConfig


def _config(video: dict) -> VideoConfig:
    """Build the VideoConfig the session builds from open_session.video."""
    return VideoConfig(
        codec=video["codec"],
        extradata=base64.b64decode(video["extradata_b64"]) if video.get("extradata_b64") else None,
        width=video["width"],
        height=video["height"],
        time_base=Fraction(*video["time_base"]),
        avg_rate=Fraction(*video["avg_rate"]) if video.get("avg_rate") else None,
    )


def run_passthrough(source, display, tier="lossless-ffv1", **pipeline_kwargs) -> bytes:
    """Relay one epoch of ``source`` and return the downlink container bytes."""
    track = VideoTrack(str(source))
    emitted: list[MediaPacket] = []
    errors: list[str] = []
    done = threading.Event()

    def emit(packet: MediaPacket) -> None:
        emitted.append(packet)
        if packet.eos:
            done.set()

    def on_error(message: str) -> None:
        errors.append(message)
        done.set()

    pipeline = Pipeline(
        _config(track.open_session_video_dict()), None, tier, display,
        emit, on_error, ep="cpu", **pipeline_kwargs,
    )
    try:
        for info in track.packets():
            pipeline.feed(track.media_packet(info, 0))
        pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=0))
        assert done.wait(30), "pipeline produced no EOS"
    finally:
        pipeline.close()
        track.close()
    assert errors == []
    return b"".join(packet.payload for packet in emitted)


def test_ffv1_source_decodes_with_container_dimensions(tmp_path):
    # FFV1 carries no frame dimensions in its bitstream; the decoder needs the
    # container's width/height, which reach the server as open_session fields.
    source = tmp_path / "ffv1.mkv"
    expected = []
    with av.open(str(source), "w") as output:
        stream = output.add_stream("ffv1", rate=24)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        for position in range(12):
            frame = av.VideoFrame(32, 32, "yuv420p")
            for index, plane in enumerate(frame.planes):
                plane.update(bytes([16 + position * 8 + index]) * plane.buffer_size)
            frame.pts, frame.time_base = position, Fraction(1, 24)
            expected.append(frame.to_ndarray().copy())
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))

    downlink = run_passthrough(source, (32, 32))

    with av.open(io.BytesIO(downlink)) as relayed:
        actual = [frame.to_ndarray() for frame in relayed.decode(video=0)]
    assert len(actual) == len(expected)
    assert all(np.array_equal(a, b) for a, b in zip(actual, expected))


# Millisecond timestamps that no constant frame rate grid reproduces.
_VFR_PTS_MS = [0, 33, 83, 117, 167, 200, 250, 283, 333, 367, 417, 450]


def _write_vfr_h264(path) -> None:
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=24, options={"bf": "0", "tune": "zerolatency"})
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, 1000)
        for index, pts in enumerate(_VFR_PTS_MS):
            frame = av.VideoFrame.from_ndarray(
                np.full((32, 32, 3), index * 20, np.uint8), format="rgb24",
            )
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))


def _video_pts_seconds(source) -> list[float]:
    with av.open(source) as container:
        stream = container.streams.video[0]
        return sorted(
            round(float(packet.pts * stream.time_base), 3)
            for packet in container.demux(stream) if packet.pts is not None
        )


def test_variable_frame_rate_timestamps_survive_the_relay(tmp_path):
    source = tmp_path / "vfr.mkv"
    _write_vfr_h264(source)
    expected = [pts / 1000 for pts in _VFR_PTS_MS]
    assert _video_pts_seconds(str(source)) == expected

    downlink = run_passthrough(source, (32, 32))

    assert _video_pts_seconds(io.BytesIO(downlink)) == expected


def test_variable_frame_rate_timestamps_survive_offline_encoding(tmp_path):
    from upscale_cli.stages import FrameSink, FrameSource

    source = tmp_path / "vfr.mkv"
    _write_vfr_h264(source)
    output = tmp_path / "out.mkv"
    with FrameSource(str(source), hwaccel="none") as frames, FrameSink(
        str(output), frames.time_base, frames.average_rate, codec="ffv1", options={},
    ) as sink:
        for frame in frames:
            sink.write(frame)

    expected = [pts / 1000 for pts in _VFR_PTS_MS]
    assert sink.pts_written == expected
    assert _video_pts_seconds(str(output)) == expected


def _wait_until_stable(read, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = read()
    while time.monotonic() < deadline:
        time.sleep(0.2)
        current = read()
        if current == last:
            return current
        last = current
    return last


def test_muxed_audio_after_the_last_video_frame_streams_before_eos(tmp_path):
    from relay_media import AuxiliaryTrack
    from relay_protocol import FLAG_DISCONTINUITY, FLAG_KEYFRAME, NO_TS

    source = tmp_path / "audio-tail.mkv"
    with av.open(str(source), "w") as output:
        video = output.add_stream("libx264", rate=24, options={"bf": "0", "tune": "zerolatency"})
        video.width = video.height = 32
        video.pix_fmt = "yuv420p"
        audio = output.add_stream("pcm_s16le", rate=48000)
        audio.layout = "stereo"
        for index in range(12):  # 0.5 s of video
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 24)
            output.mux(video.encode(frame))
        output.mux(video.encode(None))
        for index in range(120):  # 5.12 s of audio
            frame = av.AudioFrame.from_ndarray(
                np.zeros((1, 4096), np.int16), format="s16", layout="stereo",
            )
            frame.sample_rate = 48000
            frame.pts, frame.time_base = index * 2048, Fraction(1, 48000)
            output.mux(audio.encode(frame))
        output.mux(audio.encode(None))

    track = VideoTrack(str(source))
    auxiliary = AuxiliaryTrack(str(source))
    emitted: list[MediaPacket] = []
    done = threading.Event()

    def emit(packet):
        emitted.append(packet)
        if packet.eos:
            done.set()

    pipeline = Pipeline(
        _config(track.open_session_video_dict()), None, "lossless-ffv1", (32, 32),
        emit, lambda message: pytest.fail(message), ep="cpu", aux_source_path=str(source),
    )
    try:
        merged = sorted(
            [(float((i.dts if i.dts != NO_TS else i.pts) * track.time_base), 0, i)
             for i in track.packets()]
            + [(i.order_s, 1, i) for i in auxiliary.packets()],
            key=lambda item: (item[0], item[1]),
        )
        for _stamp, kind, info in merged:
            if kind == 0:
                pipeline.feed(track.media_packet(info, 0))
            else:
                pipeline.feed_aux(info, 0)
        tail = _wait_until_stable(lambda: sum(len(p.payload) for p in emitted))
        first_video_chunk = emitted[0]
        undrained = sum(len(chunk) for chunk in pipeline._sink_buf._chunks)
        pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=0))
        assert done.wait(10)
    finally:
        pipeline.close()
        auxiliary.close()
        track.close()

    total = sum(len(p.payload) for p in emitted)
    # The audio after the last frame streamed out under backpressure instead
    # of waiting in the mux buffer for end of stream.
    assert undrained < 64 * 1024
    assert tail > 0.9 * total
    # The epoch still opens on the first video chunk, which alone is marked.
    assert first_video_chunk.flags == FLAG_DISCONTINUITY | FLAG_KEYFRAME
    assert first_video_chunk.pts == 0
    assert sum(bool(p.flags & FLAG_DISCONTINUITY) for p in emitted) == 1
    # Audio-only chunks name no video frame.
    assert any(p.pts == NO_TS and not p.eos for p in emitted)
    with av.open(io.BytesIO(b"".join(p.payload for p in emitted))) as relayed:
        assert sum(f.samples for f in relayed.decode(audio=0)) == 120 * 2048
