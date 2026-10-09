"""Anamorphic sources are fitted by their displayed shape (#7).

A 720x576 PAL DVD with 16:15 pixels displays at 4:3. Fitting its stored
pixels into 1920x1080 gave 1350x1080 with no aspect signalling, shown at 5:4.
"""

from __future__ import annotations

import asyncio
import io
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig
from relay_media import VideoTrack
from relay_server.pipeline import Pipeline, VideoConfig
from relay_server.server import RelayServer
from relay_server.session import _sample_aspect_ratio
from upscale_cli.fit import cover_crop_box, fit_dimensions

ROOT = Path(__file__).resolve().parents[1]
PAL_SAR = Fraction(16, 15)


@pytest.fixture(scope="module")
def anamorphic_source(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("anamorphic") / "pal-4x3.mkv"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=25, options={"bf": "0", "g": "25"})
        stream.width, stream.height, stream.pix_fmt = 720, 576, "yuv420p"
        stream.codec_context.sample_aspect_ratio = PAL_SAR
        picture = np.zeros((576, 720, 3), np.uint8)
        picture[:, 360:] = 255  # stored left half black, right half white
        for index in range(25):
            frame = av.VideoFrame.from_ndarray(picture, format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 25)
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    return path


def test_fit_and_cover_use_the_displayed_shape():
    assert fit_dimensions(720, 576, 1920, 1080) == (1350, 1080)  # square pixels
    assert fit_dimensions(720, 576, 1920, 1080, sample_aspect_ratio=PAL_SAR) == (1440, 1080)
    # Cover to 16:9: the 768x576 display shape loses height, never stored width.
    assert cover_crop_box(720, 576, 1920, 1080, sample_aspect_ratio=PAL_SAR) == (0, 72, 720, 432)
    assert cover_crop_box(720, 576, 1920, 1080) == cover_crop_box(
        720, 576, 1920, 1080, sample_aspect_ratio=Fraction(1),
    )


@pytest.mark.parametrize(("raw", "expected"), [
    ([16, 15], PAL_SAR),
    ([32, 30], PAL_SAR),
    (None, Fraction(1)),
    ([0, 1], Fraction(1)),
    ([16, 0], Fraction(1)),
    ([-16, 15], Fraction(1)),
    (["16", "15"], Fraction(1)),
    ([16.0, 15], Fraction(1)),
    ([True, 1], Fraction(1)),
    ([16, 15, 1], Fraction(1)),
    ("16:15", Fraction(1)),
    ([1000, 1], Fraction(1)),  # beyond any real pixel shape
])
def test_open_session_sample_aspect_ratio_is_optional_and_validated(raw, expected):
    video = {} if raw is None else {"sample_aspect_ratio": raw}
    assert _sample_aspect_ratio(video) == expected


def test_source_description_carries_the_sample_aspect_ratio(anamorphic_source, tmp_path):
    track = VideoTrack(str(anamorphic_source))
    try:
        assert track.open_session_video_dict()["sample_aspect_ratio"] == [16, 15]
    finally:
        track.close()
    from upscale_cli.sample import make_sample

    square = tmp_path / "square.mkv"
    make_sample(str(square), frames=2, width=64, height=48, fps=24)
    track = VideoTrack(str(square))
    try:
        assert "sample_aspect_ratio" not in track.open_session_video_dict()
    finally:
        track.close()


@pytest.mark.parametrize(("fit_mode", "crop"), [("fit", None), ("cover", (0, 72, 720, 432))])
def test_pipeline_geometry(fit_mode, crop):
    def build(sar):
        return Pipeline(
            VideoConfig("h264", None, 720, 576, Fraction(1, 25), sample_aspect_ratio=sar),
            None, "lossless-ffv1", (1920, 1080), lambda _p: None, lambda _m: None,
            fit_mode=fit_mode,
        )

    legacy = build(Fraction(1))  # a client that sends no sample_aspect_ratio
    pipeline = build(PAL_SAR)
    try:
        if fit_mode == "fit":
            assert (legacy.out_w, legacy.out_h) == (1350, 1080)
            assert (pipeline.out_w, pipeline.out_h) == (1440, 1080)
        else:
            assert (pipeline.out_w, pipeline.out_h) == (1920, 1080)
            assert pipeline._crop_box == crop
    finally:
        legacy.close()
        pipeline.close()


@pytest.mark.parametrize("source", ["uplink", "server_file"])
def test_anamorphic_source_relays_as_square_pixels(anamorphic_source, source):
    async def scenario():
        server = RelayServer(
            str(ROOT / "models"), free_port_pair(),
            library_roots=[str(anamorphic_source.parent)] if source == "server_file" else None,
        )
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            info = await client.open_session(SessionConfig(
                path=str(anamorphic_source) if source == "uplink" else anamorphic_source.name,
                model="passthrough", quality_tier="lossless-ffv1",
                display_w=1920, display_h=1080, source=source,
            ))
            await client.attach_media()
            if source == "uplink":
                await client.start_uplink()
            await client.play()
            queue = client.downlink_queue()
            chunks = []
            while True:
                packet = await asyncio.wait_for(queue.get(), 30)
                if packet is None or packet.eos:
                    break
                chunks.append(packet.payload)
                client.buffered_ms = 0
            return info, b"".join(chunks)
        finally:
            await client.teardown()
            await server.stop()

    info, downlink = asyncio.run(scenario())
    assert (info.downlink_width, info.downlink_height) == (1440, 1080)
    with av.open(io.BytesIO(downlink)) as relayed:
        stream = relayed.streams.video[0]
        assert stream.sample_aspect_ratio in (Fraction(0), Fraction(1))  # square
        frame = next(relayed.decode(stream))
        picture = frame.to_ndarray(format="rgb24")
    assert picture.shape == (1080, 1440, 3)
    # The stored half/half split stays half/half: only the width scale changed.
    assert picture[:, :700].mean() < 10 and picture[:, 740:].mean() > 245
