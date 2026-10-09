"""A superseded epoch's demux worker must not retire the new epoch's
auxiliary iterator.

Cancelling the server source task does not stop a to_thread(next_batch) that
is already running. The worker below is held just before it opens its
auxiliary iterator until the replacement epoch has started streaming audio,
which is the interleaving that silently dropped audio and subtitles for the
rest of an epoch.
"""

from __future__ import annotations

import asyncio
import threading
from fractions import Fraction

import av
import numpy as np

from relay_media import AuxiliaryTrack, VideoTrack
from relay_server.session import Session


def _write_source(path) -> None:
    with av.open(str(path), "w") as output:
        video = output.add_stream("libx264", rate=24, options={"bf": "0", "g": "24"})
        video.width = video.height = 32
        video.pix_fmt = "yuv420p"
        audio = output.add_stream("pcm_s16le", rate=48000)
        audio.layout = "stereo"
        for index in range(72):  # 3 s of video, a keyframe every second
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 24)
            output.mux(video.encode(frame))
        output.mux(video.encode(None))
        for index in range(141):  # ~3 s of audio
            frame = av.AudioFrame.from_ndarray(
                np.zeros((1, 2048), np.int16), format="s16", layout="stereo",
            )
            frame.sample_rate = 48000
            frame.pts, frame.time_base = index * 1024, Fraction(1, 48000)
            output.mux(audio.encode(frame))
        output.mux(audio.encode(None))


class _Ws:
    async def send_str(self, _text):
        pass


class _RecordingPipeline:
    seek_discard_max_s = None

    def __init__(self, on_aux):
        self.video: list[tuple[int, object]] = []
        self.aux: list[int] = []
        self.eos = threading.Event()
        self._on_aux = on_aux

    def feed(self, packet):
        if packet.eos:
            if packet.epoch == 1:
                self.eos.set()
            return
        self.video.append((packet.epoch, packet.pts))

    def feed_aux(self, _info, epoch):
        self._on_aux(epoch)
        self.aux.append(epoch)


def test_superseded_worker_leaves_the_new_epochs_auxiliary_iterator_alone(tmp_path):
    source = tmp_path / "source.mkv"
    _write_source(source)
    seek_s = 1.0
    reference = AuxiliaryTrack(str(source))
    try:
        expected_aux = len(list(reference.packets(seek_s)))
    finally:
        reference.close()

    async def run() -> _RecordingPipeline:
        old_waiting = threading.Event()
        release_old = threading.Event()
        old_opened = threading.Event()
        new_streaming = threading.Event()

        def on_aux(epoch):
            if epoch == 1 and not new_streaming.is_set():
                new_streaming.set()
                # Hold the new epoch here until the stale worker has opened
                # its iterator, so the interleaving is deterministic.
                assert old_opened.wait(10)

        session = Session(_Ws(), {})
        session.source_track = VideoTrack(str(source))
        session.aux_track = aux = AuxiliaryTrack(str(source))
        session.pipeline = pipeline = _RecordingPipeline(on_aux)
        real_packets = aux.packets
        calls = 0

        def packets(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:  # epoch 0's worker, about to be superseded
                old_waiting.set()
                assert release_old.wait(10)
                try:
                    return real_packets(*args, **kwargs)
                finally:
                    old_opened.set()
            return real_packets(*args, **kwargs)

        aux.packets = packets
        try:
            await session.start_server_source(None)
            assert await asyncio.to_thread(old_waiting.wait, 10)
            # The seek: cancel epoch 0's task (its worker keeps running) and
            # start epoch 1 from one second in.
            session.epoch = 1
            await session._stop_server_source()
            from_pts = int(seek_s / session.source_track.time_base)
            await session.start_server_source(from_pts, discontinuity=True)
            assert await asyncio.to_thread(new_streaming.wait, 10)
            release_old.set()
            assert await asyncio.to_thread(pipeline.eos.wait, 10)
            await session._stop_server_source()
        finally:
            release_old.set()
            old_opened.set()
            session.source_track.close()
            aux.close()
        return pipeline

    pipeline = asyncio.run(run())
    assert pipeline.aux.count(1) == expected_aux
    assert pipeline.aux.count(0) == 0
