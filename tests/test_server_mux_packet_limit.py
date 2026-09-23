from fractions import Fraction
import io

import av
import numpy as np

from relay_protocol import FLAG_DISCONTINUITY
from relay_server.pipeline import Pipeline, _SinkBuffer
import relay_server.pipeline as pipeline_mod


def test_large_lossless_flushes_split_without_losing_bytes_or_repeating_discontinuity(monkeypatch):
    media = io.BytesIO()
    expected = []
    with av.open(media, "w", format="matroska") as output:
        stream = output.add_stream("ffv1", rate=24)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        for position in range(4):
            frame = av.VideoFrame(32, 32, "yuv420p")
            for index, plane in enumerate(frame.planes):
                plane.update(bytes([position * 40 + index]) * plane.buffer_size)
            frame.pts, frame.time_base = position, Fraction(1, 24)
            expected.append(frame.to_ndarray().copy())
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    original = media.getvalue()
    # A small bound exercises fragmentation without allocating giant fixtures.
    monkeypatch.setattr(pipeline_mod, "MAX_PAYLOAD_BYTES", 127)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline._sink_buf = _SinkBuffer()
    pipeline._finish_trace = None
    emitted = []
    pipeline.emit = emitted.append
    for epoch in range(3):
        emitted.clear()
        pipeline._need_discontinuity = True
        pipeline._sink_buf.write(original)
        pipeline._flush_chunk(epoch, 7000, keyframe=True)
        assert len(emitted) > 1
        assert all(len(packet.payload) <= 127 for packet in emitted)
        assert all(packet.epoch == epoch and packet.pts == 7000 for packet in emitted)
        assert emitted[0].flags & FLAG_DISCONTINUITY
        assert all(not packet.discontinuity for packet in emitted[1:])
        joined = b"".join(packet.payload for packet in emitted)
        assert joined == original
        with av.open(io.BytesIO(joined)) as source:
            actual = [frame.to_ndarray() for frame in source.decode(video=0)]
        assert len(actual) == len(expected)
        assert all(np.array_equal(a, b) for a, b in zip(actual, expected))
