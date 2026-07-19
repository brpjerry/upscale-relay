"""Chapter metadata: demux extraction, protocol echo, and UI helpers."""

import asyncio
import shutil
import socket
import sys
from fractions import Fraction
from pathlib import Path

import av
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop_client.chapters import (
    Chapter,
    chapter_index,
    normalize_chapters,
    slider_fractions,
    step_target,
)
from relay_client_core import RelayClient, SessionConfig
from relay_media import VideoTrack
from relay_server.server import RelayServer
from relay_server.session import _sanitize_chapters

_NS = Fraction(1, 1_000_000_000)
CHAPTERS_NS = [
    (0, 800_000_000, "Opening"),
    (800_000_000, 1_600_000_000, "Middle"),
    (1_600_000_000, None, None),  # untitled, open-ended
]


_next_port = [0]


def free_port_pair() -> int:
    """Walk a private, non-ephemeral range for an unused adjacent pair."""
    import random

    if _next_port[0] == 0:
        _next_port[0] = random.randrange(40000, 48000, 2)
    for _ in range(200):
        candidate = _next_port[0]
        _next_port[0] += 2
        try:
            with socket.socket() as control, socket.socket() as media:
                control.bind(("127.0.0.1", candidate))
                media.bind(("127.0.0.1", candidate + 1))
            return candidate
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture(scope="module")
def chaptered_file(tmp_path_factory) -> str:
    """The standard sample remuxed with three Matroska chapters."""
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    path = tmp_path_factory.mktemp("chapters") / "chaptered.mkv"
    with av.open(str(sample)) as src, av.open(str(path), "w") as out:
        in_stream = src.streams.video[0]
        out_stream = out.add_stream_from_template(in_stream)
        for packet in src.demux(in_stream):
            if packet.dts is None:
                continue
            packet.stream = out_stream
            out.mux(packet)
        out.set_chapters([
            {
                "id": index + 1,
                "start": start,
                "end": end if end is not None else start + 1,
                "time_base": _NS,
                "metadata": {"title": title} if title else {},
            }
            for index, (start, end, title) in enumerate(CHAPTERS_NS)
        ])
    return str(path)


def test_video_track_chapters(chaptered_file):
    track = VideoTrack(chaptered_file)
    try:
        chapters = track.chapters()
    finally:
        track.close()
    assert [c["start_s"] for c in chapters] == [0.0, 0.8, 1.6]
    assert [c["title"] for c in chapters] == ["Opening", "Middle", None]
    assert chapters[0]["end_s"] == pytest.approx(0.8)


def test_video_track_without_chapters():
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        pytest.skip("sample not generated yet")
    track = VideoTrack(str(sample))
    try:
        assert track.chapters() == []
    finally:
        track.close()


def test_sanitize_chapters_filters_junk():
    raw = [
        {"start_s": 5, "title": "B"},
        {"start_s": 0.0, "end_s": 5, "title": None},
        {"start_s": -1.0, "title": "negative"},
        {"start_s": "zero"},
        "not-a-dict",
        {"title": "no start"},
    ]
    assert _sanitize_chapters(raw) == [
        {"start_s": 0.0, "end_s": 5.0, "title": None},
        {"start_s": 5.0, "end_s": None, "title": "B"},
    ]
    assert _sanitize_chapters("nonsense") == []
    assert _sanitize_chapters(None) == []


def test_uplink_session_echoes_chapters(chaptered_file):
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path=chaptered_file, model="passthrough",
                display_w=320, display_h=180,
            ))
            assert session.chapters is not None
            assert [c["start_s"] for c in session.chapters] == [0.0, 0.8, 1.6]
            assert session.chapters[0]["title"] == "Opening"
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_server_file_session_extracts_chapters(chaptered_file, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    shutil.copy2(chaptered_file, library / "Chaptered.mkv")

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(),
                             library_root=str(library))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Chaptered.mkv", model="passthrough",
                display_w=320, display_h=180, source="server_file",
            ))
            assert session.chapters is not None
            assert [c["title"] for c in session.chapters] == ["Opening", "Middle", None]
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


# -- desktop_client.chapters helpers ----------------------------------------


def test_normalize_chapters_titles_and_order():
    chapters = normalize_chapters([
        {"start_s": 60.0, "title": None},
        {"start_s": 0.0, "title": "Intro"},
        {"start_s": -3.0, "title": "bad"},
        {"start_s": "x", "title": "bad"},
    ])
    assert chapters == [Chapter(0.0, "Intro"), Chapter(60.0, "Chapter 2")]
    assert normalize_chapters(None) == []
    assert normalize_chapters([]) == []


def test_chapter_index():
    chapters = [Chapter(10.0, "a"), Chapter(20.0, "b")]
    assert chapter_index(chapters, 0.0) is None
    assert chapter_index(chapters, 10.0) == 0
    assert chapter_index(chapters, 19.9) == 0
    assert chapter_index(chapters, 25.0) == 1
    assert chapter_index([], 5.0) is None


def test_step_target():
    chapters = [Chapter(0.0, "a"), Chapter(60.0, "b"), Chapter(120.0, "c")]
    # next
    assert step_target(chapters, 0.0, 1) == 60.0
    assert step_target(chapters, 70.0, 1) == 120.0
    assert step_target(chapters, 125.0, 1) is None
    # previous: restart the current chapter when well into it...
    assert step_target(chapters, 70.0, -1) == 60.0
    # ...but go to the one before within the opening seconds.
    assert step_target(chapters, 61.0, -1) == 0.0
    assert step_target(chapters, 1.0, -1) == 0.0
    # before the first chapter of a file whose chapters start later
    later = [Chapter(30.0, "a")]
    assert step_target(later, 10.0, -1) is None
    assert step_target(later, 10.0, 1) == 30.0
    assert step_target([], 10.0, 1) is None


def test_slider_fractions():
    chapters = [Chapter(0.0, "a"), Chapter(30.0, "b"), Chapter(90.0, "c")]
    assert slider_fractions(chapters, 120.0) == [0.25, 0.75]  # 0 and >=duration dropped
    assert slider_fractions(chapters, None) == []
    assert slider_fractions([], 120.0) == []
