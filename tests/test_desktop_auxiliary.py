"""Real mpv playback for originals with no audio track."""

import asyncio
from fractions import Fraction
import io
import os
from types import SimpleNamespace

import av
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtWidgets import QApplication
from qt_helpers import playback_loop

try:
    from desktop_client.mpv_view import MpvPlayerView
except (ImportError, OSError):
    pytest.skip("native desktop playback requires python-mpv and libmpv", allow_module_level=True)
from desktop_client.options import DesktopOptions
from relay_media.demux import VideoTrack
from relay_protocol import FLAG_DISCONTINUITY, FLAG_EOS, MediaPacket
from upscale_cli.sample import make_sample


@pytest.mark.parametrize("with_subtitles", [False, True])
def test_video_only_and_subtitle_only_originals_play_without_audio(tmp_path, with_subtitles):
    video_path = tmp_path / "video.mkv"
    make_sample(str(video_path), frames=72, width=64, height=64, fps=24)
    source_path = video_path
    if with_subtitles:
        source_path = tmp_path / "subtitles.mkv"
        srt = b"1\n00:00:00,000 --> 00:00:03,000\nSubtitle-only source\n"
        with (
            av.open(str(video_path)) as video_input,
            av.open(io.BytesIO(srt), format="srt") as subtitle_input,
            av.open(str(source_path), "w") as output,
        ):
            video = output.add_stream_from_template(video_input.streams.video[0])
            subtitle = output.add_stream_from_template(subtitle_input.streams.subtitles[0])
            for container, stream, target in (
                (video_input, video_input.streams.video[0], video),
                (subtitle_input, subtitle_input.streams.subtitles[0], subtitle),
            ):
                for packet in container.demux(stream):
                    if packet.dts is not None:
                        packet.stream = target
                        output.mux(packet)
    track = VideoTrack(str(source_path))
    try:
        assert not track.has_audio_tracks
        assert track.has_subtitle_tracks == with_subtitles
        assert track.has_auxiliary_tracks == with_subtitles
    finally:
        track.close()

    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-desktop-auxiliary",
    ))
    errors = []
    player.failed.connect(errors.append)

    async def scenario():
        queue = asyncio.Queue()
        queue.put_nowait(MediaPacket(video_path.read_bytes(), flags=FLAG_DISCONTINUITY))
        queue.put_nowait(MediaPacket(b"", flags=FLAG_EOS))
        player.start(
            SimpleNamespace(downlink_container="matroska"), queue, Fraction(1, 1000),
            source_path=str(source_path) if track.has_auxiliary_tracks else None,
            avg_rate=Fraction(24), source_has_audio=track.has_audio_tracks,
        )
        try:
            async with asyncio.timeout(5):
                while not player._tracks_reported or (player.mpv.time_pos or 0) < 0.1:
                    assert not errors
                    await asyncio.sleep(0.05)
            assert not errors
            assert player._external_attach_started == with_subtitles
            assert player.mpv.vid == 1  # the relay remains the selected video
            if with_subtitles:
                assert player.mpv.sid == 1
                assert any(t["type"] == "sub" for t in player.mpv.track_list)
        finally:
            player.stop()
            await asyncio.sleep(0)

    try:
        with playback_loop(app) as loop:
            loop.run_until_complete(scenario())
    finally:
        player.mpv.terminate()
        player.close()
