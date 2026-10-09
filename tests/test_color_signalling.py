"""Output colour is tagged and converted as the source describes it (#16).

A full-range BT.709 source used to come out untagged, so players read it as
limited-range and (for SD sizes) BT.601, changing the displayed colour even
with lossless passthrough. Untagged sources are resolved as mpv would for
their own size, not the upscaled one.
"""

from __future__ import annotations

import asyncio
import io
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
from av.video.reformatter import VideoReformatter

from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig
from relay_server.server import RelayServer
from test_server_pipeline_media import run_passthrough
from upscale_cli.color import VideoColor

ROOT = Path(__file__).resolve().parents[1]
_SWS = {1: 1, 4: 4, 5: 5, 6: 5, 7: 7, 9: 9, 10: 9}
FULL_709 = {"colorprim": "bt709", "transfer": "bt709", "colormatrix": "bt709", "fullrange": "on"}


def _write(path, size, rgb, x264_vui=None, pix_fmt="yuv444p", audio=False, frames=6):
    """Constant-colour H.264 source; the VUI is left unspecified unless given."""
    width, height = size
    params = ":".join(f"{k}={v}" for k, v in (x264_vui or {}).items())
    with av.open(str(path), "w") as output:
        options = {"crf": "0", "bf": "0"}
        if params:
            options["x264-params"] = params
        stream = output.add_stream("libx264", rate=24, options=options)
        stream.width, stream.height, stream.pix_fmt = width, height, pix_fmt
        if x264_vui and x264_vui.get("fullrange") == "on":
            stream.codec_context.color_range = 2
        if audio:
            sound = output.add_stream("pcm_s16le", rate=48000)
            sound.layout = "stereo"
        for index in range(frames):
            frame = av.VideoFrame.from_ndarray(np.full((height, width, 3), rgb, np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 24)
            if audio:  # audio first in the file, ahead of each frame
                chunk = av.AudioFrame.from_ndarray(np.zeros((1, 4000), np.int16), format="s16", layout="stereo")
                chunk.sample_rate = 48000
                chunk.pts, chunk.time_base = index * 2000, Fraction(1, 48000)
                output.mux(sound.encode(chunk))
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))


def _tags(frame) -> tuple[int, int, int, int]:
    return (int(frame.colorspace), int(frame.color_range),
            int(frame.color_primaries), int(frame.color_trc))


def _shown_rgb(frame, matrix=None, color_range=None) -> np.ndarray:
    """The centre pixel as a player reads the frame (its own tags by default)."""
    rgb = VideoReformatter().reformat(
        frame, format="rgb24",
        src_colorspace=_SWS[matrix if matrix is not None else int(frame.colorspace)],
        src_color_range=color_range if color_range is not None else int(frame.color_range),
    ).to_ndarray()
    return rgb[rgb.shape[0] // 2, rgb.shape[1] // 2].astype(int)


def _first_frame(source) -> av.VideoFrame:
    with av.open(source) as container:
        return next(container.decode(video=0))


@pytest.mark.parametrize(("tags", "size", "expected"), [
    ((2, 0, 2, 2), (720, 576), (5, 1, 5, 1)),     # PAL
    ((2, 0, 2, 2), (720, 480), (6, 1, 6, 1)),     # NTSC
    ((2, 0, 2, 2), (640, 360), (6, 1, 1, 1)),     # other SD: mpv picks BT.709 primaries
    ((2, 0, 2, 2), (1280, 544), (1, 1, 1, 1)),    # scope-cropped 720p is HD
    ((2, 0, 2, 2), (1920, 1080), (1, 1, 1, 1)),
    ((1, 2, 1, 1), (720, 576), (1, 2, 1, 1)),     # explicit tags win
    ((9, 1, 9, 16), (3840, 2160), (9, 1, 9, 16)), # HDR tags are carried
    ((0, 2, 2, 2), (1920, 1080), (1, 2, 1, 1)),   # RGB source: YUV matrix guessed
])
def test_unspecified_tags_resolve_like_mpv(tags, size, expected):
    color = VideoColor.resolve_values(*tags, *size)
    assert (color.matrix, color.range, color.primaries, color.transfer) == expected


def test_full_range_bt709_survives_passthrough(tmp_path):
    source = tmp_path / "bt709-full.mkv"
    _write(source, (64, 64), (180, 80, 130), FULL_709)
    original = _first_frame(str(source))
    assert _tags(original) == (1, 2, 1, 1)

    relayed = _first_frame(io.BytesIO(run_passthrough(source, (64, 64))))

    assert _tags(relayed) == (1, 2, 1, 1)
    assert np.abs(_shown_rgb(relayed) - _shown_rgb(original)).max() <= 2


def test_untagged_sd_source_is_tagged_as_mpv_reads_it(tmp_path):
    source = tmp_path / "pal.mkv"
    _write(source, (720, 576), (200, 60, 90), pix_fmt="yuv420p", frames=2)
    original = _first_frame(str(source))
    assert _tags(original) == (2, 0, 2, 2)

    relayed = _first_frame(io.BytesIO(run_passthrough(source, (1920, 1080))))

    assert (relayed.width, relayed.height) == (1350, 1080)  # HD-sized output...
    assert _tags(relayed) == (5, 1, 5, 1)                   # ...still read as PAL
    expected = _shown_rgb(original, matrix=5, color_range=1)
    assert np.abs(_shown_rgb(relayed) - expected).max() <= 2


def test_model_output_is_converted_with_the_source_matrix(tmp_path):
    pytest.importorskip("onnxruntime")
    import sys

    sys.path.insert(0, str(ROOT))
    from tools.make_test_model import build
    from relay_server.pipeline import Pipeline
    from test_server_pipeline_media import _config
    from relay_media import VideoTrack
    from relay_protocol import FLAG_EOS, MediaPacket
    import threading

    model = tmp_path / "bilinear2x.onnx"
    build(model)
    source = tmp_path / "bt709-full.mkv"
    _write(source, (64, 64), (180, 80, 130), FULL_709)
    track = VideoTrack(str(source))
    emitted, done = [], threading.Event()

    def emit(packet):
        emitted.append(packet)
        if packet.eos:
            done.set()

    pipeline = Pipeline(_config(track.open_session_video_dict()), str(model), "lossless-ffv1",
                        (128, 128), emit, lambda message: pytest.fail(message), ep="cpu")
    try:
        for info in track.packets():
            pipeline.feed(track.media_packet(info, 0))
        pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=0))
        assert done.wait(30)
    finally:
        pipeline.close()
        track.close()
    relayed = _first_frame(io.BytesIO(b"".join(p.payload for p in emitted)))
    assert (relayed.width, relayed.height) == (128, 128)
    assert _tags(relayed) == (1, 2, 1, 1)
    assert np.abs(_shown_rgb(relayed) - _shown_rgb(_first_frame(str(source)))).max() <= 2


def test_server_file_with_muxed_audio_is_tagged_from_the_first_byte(tmp_path):
    # Audio packets reach the muxer before the first video frame, which makes
    # the container write its header (and open the encoder) early.
    _write(tmp_path / "with-audio.mkv", (64, 64), (180, 80, 130), FULL_709, audio=True)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_roots=[str(tmp_path)])
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            info = await client.open_session(SessionConfig(
                path="with-audio.mkv", source="server_file", aux_tracks="muxed",
                model="passthrough", quality_tier="lossless-ffv1", display_w=64, display_h=64,
            ))
            assert info.aux_tracks == "muxed"
            await client.attach_media()
            await client.play()
            queue, chunks = client.downlink_queue(), []
            while True:
                packet = await asyncio.wait_for(queue.get(), 30)
                if packet is None or packet.eos:
                    break
                chunks.append(packet.payload)
                client.buffered_ms = 0
            return b"".join(chunks)
        finally:
            await client.teardown()
            await server.stop()

    with av.open(io.BytesIO(asyncio.run(scenario()))) as relayed:
        stream = relayed.streams.video[0]
        assert _tags(stream.codec_context) == (1, 2, 1, 1)  # container + bitstream header
        assert _tags(next(relayed.decode(stream))) == (1, 2, 1, 1)


def test_offline_encoding_keeps_the_source_colour(tmp_path):
    from upscale_cli.stages import FrameSink, FrameSource

    source = tmp_path / "bt709-full.mkv"
    _write(source, (64, 64), (180, 80, 130), FULL_709)
    output = tmp_path / "out.mkv"
    with FrameSource(str(source), hwaccel="none") as frames, FrameSink(
        str(output), frames.time_base, frames.average_rate, codec="ffv1", options={},
        color=frames.color,
    ) as sink:
        for frame in frames:
            sink.write(frame)

    encoded = _first_frame(str(output))
    assert _tags(encoded) == (1, 2, 1, 1)
    assert np.abs(_shown_rgb(encoded) - _shown_rgb(_first_frame(str(source)))).max() <= 2
