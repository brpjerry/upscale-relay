from __future__ import annotations

import logging
from types import SimpleNamespace

from relay_server.server import RelayServer


def test_performance_snapshot_includes_status_fields(caplog):
    stats = SimpleNamespace(
        fps=23.5,
        frames_in=120,
        frames_out=118,
        paused_for_backpressure=False,
        stage_report=lambda: {"decode": 3.2, "infer": 12.5, "fit_encode": 8.1},
    )
    pipeline = SimpleNamespace(
        stats=stats,
        queue_depths=lambda: {"in": 2, "decoded": 3, "upscaled": 1},
        client_buffered_ms=8400,
        buffered_ms_now=lambda: 8123.0,
        out_w=3840,
        out_h=2160,
        downlink_codec="hevc",
        encoder_name="hevc_nvenc",
        quality_tier="hevc-qp4",
    )
    session = SimpleNamespace(
        id="abcdef123456",
        state=SimpleNamespace(value="playing"),
        epoch=2,
        source_kind="server_file",
        pipeline=pipeline,
        down_q=SimpleNamespace(qsize=lambda: 4),
    )

    with caplog.at_level(logging.INFO, logger="relay.stats"):
        RelayServer._log_session_stats(session, final=True)

    message = caplog.records[-1].getMessage()
    assert "abcdef playing FINAL epoch=2 source=server_file" in message
    assert "pipeline  23.5 fps" in message
    assert "onnx  80.0 fps" in message
    assert "decode=3.2ms infer=12.5ms fit_encode=8.1ms" in message
    assert "output=3840x2160 codec=hevc encoder=hevc_nvenc tier=hevc-qp4" in message
    assert "client buffer  8400 ms (est  8123)" in message
    assert "frames 120 in / 118 out" in message
