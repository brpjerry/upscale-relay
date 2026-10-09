"""Real decode -> encode -> mux runs of the server pipeline on generated media.

Each test writes a small source file, feeds its packets through a CPU
passthrough Pipeline exactly as the session does, and decodes the downlink
Matroska stream it produced.
"""

from __future__ import annotations

import base64
import io
import threading
from fractions import Fraction

import av
import numpy as np

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
