"""SessionConfig.passthrough_4k: 4K and larger sources open with passthrough."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig, is_4k_or_higher
from relay_client_core import client as module
from relay_server.server import RelayServer

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("size", "expected"), [
    ((3840, 2160), True),
    ((4096, 2160), True),
    ((3840, 1600), True),  # scope crop of UHD
    ((2160, 3840), True),  # portrait UHD
    ((7680, 4320), True),
    ((3838, 2158), False),
    ((2560, 1440), False),
    ((1920, 1080), False),
])
def test_4k_threshold(size, expected):
    assert is_4k_or_higher(*size) is expected


def _opened(**fields) -> dict:
    return {
        "session_id": "s", "media_port": 1, "uplink_token": None,
        "downlink_token": "d", "downlink_codec": "hevc",
        "downlink_width": 1920, "downlink_height": 1080, **fields,
    }


def _open(monkeypatch, cfg: SessionConfig, *, video=None, probe=None):
    """Run open_session against a recorded control channel; returns
    (fields sent, SessionInfo, probed URLs)."""
    probed = []
    if video is not None:
        monkeypatch.setattr(
            module, "_open_local_source",
            lambda path: (SimpleNamespace(close=lambda: None), video, None, []),
        )

    def fake_probe(url):
        probed.append(url)
        if isinstance(probe, BaseException):
            raise probe
        return probe

    monkeypatch.setattr(module, "_probe_video_size", fake_probe)

    async def scenario():
        client = RelayClient("media-server", 8590)
        sent = {}

        async def request(expect, type_, timeout=30, keepalive=False, **fields):
            sent.update(fields)
            return _opened()

        client._request = request
        try:
            return sent, await client.open_session(cfg), probed
        finally:
            await client.close()

    return asyncio.run(scenario())


UHD = {"codec": "hevc", "width": 3840, "height": 2160, "time_base": [1, 1000]}
FHD = {"codec": "h264", "width": 1920, "height": 1080, "time_base": [1, 1000]}


def test_uplink_4k_source_opens_with_passthrough(monkeypatch):
    sent, info, probed = _open(
        monkeypatch, SessionConfig("movie.mkv", model="2x_Model", passthrough_4k=True), video=UHD,
    )
    assert sent["model"] == "passthrough"
    assert info.model == "passthrough"
    assert info.source_size == (3840, 2160)
    assert probed == []  # the uplink's own demux already knew the size


def test_uplink_below_4k_keeps_the_model(monkeypatch):
    sent, info, _ = _open(
        monkeypatch, SessionConfig("movie.mkv", model="2x_Model", passthrough_4k=True), video=FHD,
    )
    assert sent["model"] == info.model == "2x_Model"
    assert info.source_size == (1920, 1080)


def test_option_off_keeps_the_model_for_4k(monkeypatch):
    sent, info, _ = _open(monkeypatch, SessionConfig("movie.mkv", model="2x_Model"), video=UHD)
    assert sent["model"] == info.model == "2x_Model"


def test_server_file_is_probed_through_media(monkeypatch):
    sent, info, probed = _open(monkeypatch, SessionConfig(
        "Films/Big Movie.mkv", model="2x_Model", source="server_file", passthrough_4k=True,
    ), probe=(4096, 1716))
    assert probed == ["http://media-server:8590/media/Films/Big%20Movie.mkv"]
    assert sent["model"] == info.model == "passthrough"
    assert info.source_size == (4096, 1716)


def test_failed_server_file_probe_keeps_the_model(monkeypatch):
    sent, info, probed = _open(monkeypatch, SessionConfig(
        "show.mkv", model="2x_Model", source="server_file", passthrough_4k=True,
    ), probe=OSError("connection refused"))
    assert len(probed) == 1
    assert sent["model"] == info.model == "2x_Model"
    assert info.source_size is None


@pytest.mark.parametrize("cfg", [
    SessionConfig("show.mkv", model="2x_Model", source="server_file"),
    SessionConfig("show.mkv", model="passthrough", source="server_file", passthrough_4k=True),
])
def test_server_file_probe_only_when_it_can_change_the_model(monkeypatch, cfg):
    sent, info, probed = _open(monkeypatch, cfg, probe=(3840, 2160))
    assert probed == []
    assert sent["model"] == cfg.model
    assert info.source_size is None


@pytest.fixture(scope="module")
def uhd_library(tmp_path_factory) -> Path:
    from upscale_cli.sample import make_sample

    library = tmp_path_factory.mktemp("library")
    (library / "Films").mkdir()
    make_sample(str(library / "Films" / "UHD.mkv"), frames=2, width=3840, height=2160, fps=24)
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    shutil.copy2(sample, library / "Films" / "Small.mkv")
    return library


def test_real_server_file_probe_selects_passthrough(uhd_library):
    """Through the real /media endpoint: a model the server does not have
    is never requested for the 4K file, and is for the small one."""

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(uhd_library))
        await server.start()
        try:
            small = RelayClient("127.0.0.1", server.port)
            try:
                await small.connect()
                with pytest.raises(RuntimeError, match="unknown_model"):
                    await small.open_session(SessionConfig(
                        "Films/Small.mkv", model="no-such-model", quality_tier="lossless-ffv1",
                        display_w=320, display_h=180, source="server_file", passthrough_4k=True,
                    ))
            finally:
                await small.close()
            client = RelayClient("127.0.0.1", server.port)
            try:
                await client.connect()
                info = await client.open_session(SessionConfig(
                    "Films/UHD.mkv", model="no-such-model", quality_tier="lossless-ffv1",
                    display_w=320, display_h=180, source="server_file", passthrough_4k=True,
                ))
                assert info.model == "passthrough"
                assert info.source_size == (3840, 2160)
            finally:
                await client.teardown()
        finally:
            await server.stop()

    asyncio.run(scenario())
