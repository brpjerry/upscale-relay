"""The pipeline's input queue bounds payload bytes, not only packet count."""

from __future__ import annotations

import threading
import time
from fractions import Fraction

import av

from relay_media import AuxiliaryPacketInfo
from relay_protocol import MediaPacket
import relay_server.pipeline as pipeline_mod
from relay_server.pipeline import HIGH_WATERMARK_MS, Pipeline, VideoConfig

_BUDGET = 8 * 1024


def _parked_pipeline(monkeypatch) -> Pipeline:
    """A pipeline whose decode stage holds its first packet in backpressure."""
    monkeypatch.setattr(pipeline_mod, "_INPUT_QUEUE_MAX_BYTES", _BUDGET)
    pipeline = Pipeline(
        VideoConfig("h264", None, 32, 32, Fraction(1, 1000)), None,
        "lossless-ffv1", (32, 32), lambda _packet: None, lambda _message: None,
    )
    pipeline.note_buffer_report(HIGH_WATERMARK_MS * 10)  # client "full": park
    return pipeline


def _fill(pipeline: Pipeline, make_item, feed) -> list[int]:
    fed: list[int] = []

    def producer():
        for index in range(64):
            feed(make_item(index))
            if pipeline._closed.is_set():
                return
            fed.append(index)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    previous = -1
    while time.monotonic() < deadline:  # wait until the producer stops advancing
        time.sleep(0.2)
        if len(fed) == previous:
            break
        previous = len(fed)
    return fed


def test_paused_pipeline_stops_accepting_video_at_the_byte_budget(monkeypatch):
    pipeline = _parked_pipeline(monkeypatch)
    try:
        fed = _fill(
            pipeline,
            lambda i: MediaPacket(payload=bytes(1024), epoch=0, pts=i, dts=i),
            pipeline.feed,
        )
        # One packet is held by the parked decode stage; the queue holds the rest.
        assert len(fed) == 1 + _BUDGET // 1024
        assert pipeline.in_q.payload_bytes == _BUDGET
    finally:
        pipeline.close()


def test_auxiliary_packets_count_against_the_same_budget(monkeypatch):
    pipeline = _parked_pipeline(monkeypatch)
    try:
        pipeline.feed(MediaPacket(payload=bytes(1024), epoch=0, pts=0, dts=0))

        def auxiliary(index):
            packet = av.Packet(1024)
            packet.pts = packet.dts = index
            return AuxiliaryPacketInfo(packet=packet, stream_index=1, order_s=float(index))

        fed = _fill(pipeline, auxiliary, lambda info: pipeline.feed_aux(info, 0))
        assert len(fed) == _BUDGET // 1024
        assert pipeline.in_q.payload_bytes == _BUDGET
    finally:
        pipeline.close()


def test_seek_and_close_release_a_blocked_feeder(monkeypatch):
    pipeline = _parked_pipeline(monkeypatch)
    try:
        fed = _fill(
            pipeline,
            lambda i: MediaPacket(payload=bytes(1024), epoch=0, pts=i, dts=i),
            pipeline.feed,
        )
        before = len(fed)
        pipeline.flush(1, 0)  # discards queued stale input, freeing the budget
        deadline = time.monotonic() + 5
        while len(fed) == before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(fed) > before
    finally:
        pipeline.close()
    # After close a feed returns instead of waiting on a queue nobody drains.
    for index in range(2 * _BUDGET // 1024):
        pipeline.feed(MediaPacket(payload=bytes(1024), epoch=1, pts=index, dts=index))


def test_one_oversized_packet_still_fits_an_empty_queue(monkeypatch):
    pipeline = _parked_pipeline(monkeypatch)
    try:
        pipeline.feed(MediaPacket(payload=bytes(1024), epoch=0, pts=0, dts=0))
        done = threading.Event()

        def feed_large():
            pipeline.feed(MediaPacket(payload=bytes(_BUDGET * 2), epoch=0, pts=1, dts=1))
            done.set()

        threading.Thread(target=feed_large, daemon=True).start()
        assert done.wait(5)
        assert pipeline.in_q.payload_bytes == _BUDGET * 2
    finally:
        pipeline.close()
