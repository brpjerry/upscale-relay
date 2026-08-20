import asyncio
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
from types import MethodType, SimpleNamespace

import pytest
from aiohttp import web

from relay_client_core.attachments import (
    materialize_attachment_cache,
    validate_manifest,
)
from relay_client_core.client import RelayClient, _take_downlink_batch
from relay_protocol import FLAG_DISCONTINUITY, MediaPacket
from relay_server.pipeline import (
    MUX_MAX_INTERLEAVE_DELTA_US,
    Pipeline,
    PipelineCloseError,
)
from relay_server.session import Session, State
import relay_server.session as session_mod
from relay_server.server import RelayServer
from upscale_cli import encode as encode_mod


class _FakeMux:
    def __init__(self, release: threading.Event | None = None):
        self.release = release
        self.closed = 0

    def close(self):
        self.closed += 1
        if self.release is not None:
            self.release.wait(5)


class _FakeUpscaler:
    def __init__(self, infer_active: threading.Event):
        self.infer_active = infer_active
        self.closed = 0

    def close(self):
        assert not self.infer_active.is_set()
        self.closed += 1


def _closing_pipeline(release: threading.Event | None = None):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline._close_lock = threading.Lock()
    pipeline._close_complete = threading.Event()
    pipeline._close_started = False
    pipeline._close_error = None
    pipeline._finish_cleanup_error = None
    pipeline._closed = threading.Event()
    pipeline._flush_pending = threading.Event()
    pipeline.in_q = queue.Queue(maxsize=4)
    pipeline._q_dec = queue.Queue(maxsize=4)
    pipeline._q_up = queue.Queue(maxsize=4)
    pipeline.playing = True
    pipeline.quality_tier = "hevc-qp6"
    pipeline.encoder_name = "hevc_nvenc"
    pipeline._mux = _FakeMux(release)
    pipeline._enc_stream = object()
    pipeline._aux_streams = {1: object()}
    pipeline._aux_template_container = None
    pipeline._decoder = object()
    pipeline._reformatter = object()
    pipeline._crop_reformatter = object()
    pipeline._in_reformatter = object()
    infer_active = threading.Event()
    infer_active.set()
    pipeline.upscaler = _FakeUpscaler(infer_active)

    def decode():
        pipeline.in_q.get()

    def infer():
        pipeline._q_dec.get()
        infer_active.clear()

    def finish():
        pipeline._q_up.get()

    decode.__name__ = "_decode_work"
    infer.__name__ = "_infer_work"
    finish.__name__ = "_finish_work"
    pipeline._threads = [
        threading.Thread(target=pipeline._guard(decode), name="fake-decode"),
        threading.Thread(target=pipeline._guard(infer), name="fake-infer"),
        threading.Thread(target=pipeline._guard(finish), name="fake-finish"),
    ]
    for thread in pipeline._threads:
        thread.start()
    return pipeline


def test_pipeline_close_is_one_native_release_barrier_for_concurrent_callers():
    release = threading.Event()
    pipeline = _closing_pipeline(release)
    results = []

    def close():
        pipeline.close(timeout=2)
        results.append("closed")

    callers = [threading.Thread(target=close) for _ in range(2)]
    for caller in callers:
        caller.start()
    time.sleep(0.05)
    assert results == []
    assert pipeline._enc_stream is not None
    release.set()
    for caller in callers:
        caller.join(2)
    assert results == ["closed", "closed"]
    assert pipeline._mux is None
    assert pipeline._enc_stream is None
    assert pipeline.upscaler is None


def test_pipeline_close_timeout_names_surviving_native_owner():
    release = threading.Event()
    pipeline = _closing_pipeline(release)
    with pytest.raises(PipelineCloseError, match="fake-finish"):
        pipeline.close(timeout=0.02)
    release.set()
    for thread in pipeline._threads:
        thread.join(2)


def test_session_close_reaps_pipeline_published_by_in_progress_open():
    async def scenario():
        class Ws:
            async def send_str(self, _value):
                pass

        session = Session(Ws(), {})
        release = asyncio.Event()
        closed = threading.Event()

        class LatePipeline:
            playing = False

            def close(self):
                closed.set()

        async def late_open():
            await release.wait()
            session.pipeline = LatePipeline()

        session._open_task = asyncio.create_task(late_open())
        close_task = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        release.set()
        await close_task
        assert closed.is_set()
        assert session.pipeline is None

    asyncio.run(scenario())


def test_client_teardown_waits_for_closed_before_local_close():
    async def scenario():
        client = RelayClient.__new__(RelayClient)
        client._uplink_task = None
        client._report_task = None
        client._ws = SimpleNamespace(closed=False)
        client._has_server_session = True
        release = asyncio.Event()
        events = []

        async def request(_self, expect, type_, timeout):
            assert (expect, type_) == ("closed", "teardown")
            events.append("requested")
            await release.wait()
            events.append("acknowledged")
            return {"type": "closed"}

        async def close(_self):
            events.append("local-close")

        client._request = MethodType(request, client)
        client.close = MethodType(close, client)
        task = asyncio.create_task(client.teardown())
        await asyncio.sleep(0)
        assert events == ["requested"]
        release.set()
        await task
        assert events == ["requested", "acknowledged", "local-close"]

    asyncio.run(scenario())


def test_server_closed_ack_waits_for_session_cleanup_barrier():
    async def scenario():
        release = asyncio.Event()

        class FakeSession:
            id = "session"
            uplink_token = "up"
            downlink_token = "down"
            pipeline = None

            async def close(self):
                await release.wait()

        class FakeWs:
            closed = False

            def __init__(self):
                self.messages = []

            async def send_str(self, value):
                self.messages.append(value)

        server = RelayServer.__new__(RelayServer)
        session = FakeSession()
        server.sessions = {"up": session, "down": session, "session": session}
        ws = FakeWs()
        task = asyncio.create_task(server._close_control_session(session, ws, True))
        await asyncio.sleep(0)
        assert ws.messages == []
        release.set()
        assert await task is True
        assert ws.messages == ['{"type": "closed"}']
        assert server.sessions == {}

    asyncio.run(scenario())


def test_server_does_not_ack_failed_native_cleanup():
    async def scenario():
        class FakeSession:
            id = "session"
            uplink_token = "up"
            downlink_token = "down"
            pipeline = None

            async def close(self):
                raise PipelineCloseError("fake-finish survived")

        class FakeWs:
            closed = False

            def __init__(self):
                self.messages = []
                self.close_code = None

            async def send_str(self, value):
                self.messages.append(value)

            async def close(self, code, message):
                self.closed = True
                self.close_code = (code, message)

        server = RelayServer.__new__(RelayServer)
        session = FakeSession()
        server.sessions = {"up": session, "down": session, "session": session}
        ws = FakeWs()
        assert await server._close_control_session(session, ws, True) is False
        assert ws.messages == []
        assert ws.close_code[0] == 1011
        assert server.native_teardown_error["restart_required"] is True
        assert "fake-finish survived" in server.native_teardown_error["error"]

    asyncio.run(scenario())


def test_attachment_endpoint_requires_manifest_token_and_hash():
    async def scenario():
        data = b"font"
        digest = hashlib.sha256(data).hexdigest()
        attachment = SimpleNamespace(data=data, mimetype="font/ttf")

        class Aux:
            def attachment_by_hash(self, value):
                return attachment if value == digest else None

        session = SimpleNamespace(
            id="s", attachment_token="secret", state=State.OPEN,
            aux_track=Aux(), attachment_manifest=[{"sha256": digest}],
        )
        server = RelayServer.__new__(RelayServer)
        server.sessions = {"s": session}
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret"},
            match_info={"digest": digest},
        )
        response = await server.handle_attachment(request)
        assert response.body == data
        request.headers = {"Authorization": "Bearer wrong"}
        with pytest.raises(web.HTTPUnauthorized):
            await server.handle_attachment(request)

    asyncio.run(scenario())


def test_cached_attachment_negotiation_omits_epoch_attachment_bodies(monkeypatch):
    async def scenario():
        messages = []
        pipeline_kwargs = {}
        digest = "b" * 64

        class Ws:
            async def send_str(self, value):
                messages.append(json.loads(value))

        class Library:
            def resolve_file(self, _path):
                return Path("fake.mkv")

        class Video:
            average_rate = Fraction(24, 1)

            def __init__(self, _path):
                pass

            def open_session_video_dict(self):
                return {
                    "codec": "h264", "width": 320, "height": 180,
                    "time_base": [1, 1000], "avg_rate": [24, 1],
                }

            def duration_seconds(self):
                return 10.0

            def chapters(self):
                return []

            def close(self):
                pass

        class Aux:
            attachment_bytes = 35 * 1024 * 1024
            attachment_cache_supported = True
            attachments = (object(),)

            def __init__(self, _path):
                pass

            def attachment_manifest(self):
                return [{
                    "name": "font.ttf", "mimetype": "font/ttf",
                    "size": 1, "sha256": digest,
                }]

            def close(self):
                pass

        class FakePipeline:
            downlink_container = "matroska"
            downlink_codec = "hevc"
            downlink_extradata_b64 = None
            out_w = 320
            out_h = 180
            fit_mode = "fit"
            resize_algorithm = "lanczos"
            playing = False

            def __init__(self, *_args, **kwargs):
                pipeline_kwargs.update(kwargs)

            def close(self):
                pass

        monkeypatch.setattr(session_mod, "VideoTrack", Video)
        monkeypatch.setattr(session_mod, "AuxiliaryTrack", Aux)
        monkeypatch.setattr(session_mod, "Pipeline", FakePipeline)
        session = Session(Ws(), {}, library=Library())
        await session.handle_open({
            "source": {"type": "server_file", "path": "fake.mkv"},
            "model": "passthrough", "quality_tier": "hevc-qp6",
            "display": {"w": 320, "h": 180},
            "aux_tracks": "muxed", "aux_attachments": "cached",
        })
        opened = next(item for item in messages if item["type"] == "session_opened")
        assert opened["aux_tracks"] == "muxed"
        assert opened["aux_attachments"] == "cached"
        assert opened["attachment_manifest"][0]["sha256"] == digest
        assert opened["attachment_token"]
        assert pipeline_kwargs["embed_aux_attachments"] is False
        await session.close()

    asyncio.run(scenario())


def test_discontinuity_packets_bypass_batch_and_drop_stale_partial_batch():
    batch = [MediaPacket(payload=b"old", epoch=0)]
    boundary = MediaPacket(payload=b"header", flags=FLAG_DISCONTINUITY, epoch=1)
    assert _take_downlink_batch(batch, boundary, 1) == [boundary]
    assert batch == []
    for index in range(7):
        assert _take_downlink_batch(
            batch, MediaPacket(payload=bytes([index]), epoch=1), 1,
        ) is None
    eighth = MediaPacket(payload=b"8", epoch=1)
    ready = _take_downlink_batch(batch, eighth, 1)
    assert ready is not None and len(ready) == 8
    assert batch == []


def test_mux_uses_small_nonzero_interleave_bound(monkeypatch):
    captured = {}

    class Mux:
        def add_stream(self, *_args, **_kwargs):
            return SimpleNamespace(width=None, height=None, pix_fmt=None)

    def fake_open(*_args, **kwargs):
        captured.update(kwargs)
        return Mux()

    monkeypatch.setattr("relay_server.pipeline.av.open", fake_open)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline._mux = None
    pipeline._sink_buf = object()
    pipeline._enc_options = {}
    pipeline._enc_codec = "fake"
    pipeline.video = SimpleNamespace(avg_rate=Fraction(24, 1))
    pipeline.out_w = 320
    pipeline.out_h = 180
    pipeline._enc_pix_fmt = "yuv420p"
    pipeline._aux_template_container = None
    pipeline._embed_aux_attachments = True
    pipeline._open_mux()
    assert captured["container_options"]["max_interleave_delta"] == str(
        MUX_MAX_INTERLEAVE_DELTA_US
    )
    assert MUX_MAX_INTERLEAVE_DELTA_US > 0


def test_encoder_selection_reports_original_probe_reason(monkeypatch):
    monkeypatch.setitem(
        encode_mod.TIERS, "diagnostic-test",
        [("hevc_nvenc", "yuv420p", {"qp": "6"})],
    )
    encode_mod._PROBE_SUCCESSES.clear()

    def fail(*_args, **_kwargs):
        raise RuntimeError("No free NVENC sessions on device")

    monkeypatch.setattr(encode_mod, "_probe_encoder_or_raise", fail)
    try:
        with pytest.raises(RuntimeError) as caught:
            encode_mod.select_encoder("diagnostic-test")
        assert "session/resource exhaustion" in str(caught.value)
        assert "No free NVENC sessions on device" in str(caught.value)
        assert isinstance(caught.value.__cause__, RuntimeError)
    finally:
        encode_mod._PROBE_SUCCESSES.clear()


def test_attachment_manifest_sanitizes_names_and_rejects_hashes():
    digest = "a" * 64
    manifest = validate_manifest([{
        "name": "../../unsafe font.ttf", "mimetype": "font/ttf",
        "size": 3, "sha256": digest,
    }])
    assert manifest[0]["name"] == "unsafe_font.ttf"
    with pytest.raises(ValueError, match="hash"):
        validate_manifest([{"name": "x", "size": 0, "sha256": "../bad"}])


class _FakeContent:
    def __init__(self, data: bytes):
        self.data = data

    async def iter_chunked(self, _size):
        yield self.data


class _FakeResponse:
    def __init__(self, data: bytes):
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass


class _FakeHttp:
    def __init__(self, data: bytes):
        self.data = data
        self.requests = 0

    def get(self, _url, headers):
        assert headers["Authorization"] == "Bearer secret"
        self.requests += 1
        return _FakeResponse(self.data)


def test_attachment_cache_downloads_once_and_reuses_verified_object(tmp_path):
    async def scenario():
        data = b"font bytes"
        digest = hashlib.sha256(data).hexdigest()
        manifest = [{
            "name": "font.ttf", "mimetype": "font/ttf",
            "size": len(data), "sha256": digest,
        }]
        http = _FakeHttp(data)
        first = await materialize_attachment_cache(
            http, "http://server", "one", manifest, "secret", tmp_path,
        )
        second = await materialize_attachment_cache(
            http, "http://server", "two", manifest, "secret", tmp_path,
        )
        assert http.requests == 1
        assert (first / "font.ttf").read_bytes() == data
        assert (second / "font.ttf").read_bytes() == data

    asyncio.run(scenario())


def test_bad_attachment_body_never_publishes_cache_entry(tmp_path):
    async def scenario():
        expected = b"expected"
        digest = hashlib.sha256(expected).hexdigest()
        manifest = [{
            "name": "font.ttf", "mimetype": "font/ttf",
            "size": len(expected), "sha256": digest,
        }]
        with pytest.raises(ValueError, match="hash"):
            await materialize_attachment_cache(
                _FakeHttp(b"corrupt!"), "http://server", "bad",
                manifest, "secret", tmp_path,
            )
        assert not (tmp_path / "objects" / digest).exists()

    asyncio.run(scenario())
