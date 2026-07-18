"""Server-hosted library discovery, media serving, and streaming tests."""

import asyncio
import shutil
import socket
from pathlib import Path

import av
import pytest

from relay_client_core import RelayClient, SessionConfig
from relay_server.library import LibraryPathError, MediaLibrary
from relay_server.server import RelayServer


ROOT = Path(__file__).resolve().parents[1]


def free_port_pair() -> int:
    import random

    # Walk a private range; do not ask the OS for an ephemeral port and then
    # release it before use (that check-then-use pattern races allocation).
    start = random.randrange(40000, 55000, 2)
    for candidate in range(start, start + 400, 2):
        try:
            with socket.socket() as control, socket.socket() as media:
                control.bind(("127.0.0.1", candidate))
                media.bind(("127.0.0.1", candidate + 1))
            return candidate
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture()
def library_file(tmp_path) -> tuple[Path, Path]:
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    library = tmp_path / "library"
    target = library / "Shows" / "Sample.MKV"
    target.parent.mkdir(parents=True)
    shutil.copy2(sample, target)
    (library / "ignore.txt").write_text("not media", encoding="utf-8")
    return library, target


def source_pts(path: Path) -> list[int]:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return sorted(packet.pts for packet in container.demux(stream) if packet.pts is not None)


def downlink_pts(packets, source_tb_den: int = 1000) -> list[int]:
    import io

    blob = b"".join(packet.payload for packet in packets if packet.payload)
    result = []
    with av.open(io.BytesIO(blob)) as container:
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.pts is not None:
                result.append(round(float(packet.pts * stream.time_base) * source_tb_den))
    return sorted(result)


async def collect(client: RelayClient):
    packets = []
    while True:
        packet = await asyncio.wait_for(client.downlink_queue().get(), timeout=60)
        assert packet is not None
        packets.append(packet)
        client.buffered_ms = 0
        if packet.eos:
            return packets


async def collect_some(client: RelayClient, count: int):
    packets = []
    for _ in range(count):
        packet = await asyncio.wait_for(client.downlink_queue().get(), timeout=60)
        assert packet is not None
        packets.append(packet)
    return packets


def test_library_tree_and_path_sandbox(library_file, tmp_path):
    root, target = library_file
    library = MediaLibrary(root)
    tree = library.tree()
    assert tree["children"][0]["name"] == "Shows"
    assert tree["children"][0]["children"] == [
        {"type": "file", "name": "Sample.MKV", "path": "Shows/Sample.MKV"}
    ]
    assert library.resolve_file("Shows/Sample.MKV") == target.resolve()
    with pytest.raises(LibraryPathError):
        library.resolve_file("../outside.mkv")
    with pytest.raises(LibraryPathError):
        library.resolve_file("Shows\\Sample.MKV")
    with pytest.raises(LibraryPathError):
        library.resolve_file("ignore.txt")


def test_library_http_range_and_server_source_pts(library_file):
    root, target = library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            caps = await client.connect()
            assert caps["library"] is True
            assert caps["default_resize_algorithm"] == "lanczos"
            assert "area" in caps["resize_algorithms"]
            assert "sinc" in caps["resize_algorithms"]
            assert caps["quality_tiers"] == [
                option["id"] for option in caps["quality_options"]
            ]
            assert caps["quality_options"][1]["label"] == "HEVC ~350 Mbps"
            assert caps["quality_options"][-1]["android_supported"] is False
            async with client._http.get(f"http://127.0.0.1:{server.port}/status") as response:
                assert (await response.json())["lossless_hevc_profile"] == "nvenc-p4-low-delay"
            tree = await client.fetch_library()
            assert tree["children"][0]["children"][0]["path"] == "Shows/Sample.MKV"

            async with client._http.get(
                client.media_url("Shows/Sample.MKV"), headers={"Range": "bytes=0-31"}
            ) as response:
                assert response.status == 206
                assert response.headers["Content-Range"].startswith("bytes 0-31/")
                assert await response.read() == target.read_bytes()[:32]

            session = await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=200, fit_mode="cover",
                resize_algorithm="area",
            ))
            assert client.track is None
            assert session.uplink_token is None
            assert session.time_base is not None
            assert session.duration_s is not None
            assert session.avg_rate is not None
            assert (session.downlink_width, session.downlink_height) == (320, 200)
            assert session.fit_mode == "cover"
            assert session.resize_algorithm == "area"
            await client.attach_media()
            await client.start_uplink()  # deliberate no-op for a server source
            await client.play()
            packets = await collect(client)
            assert packets[0].discontinuity
            assert packets[-1].eos
            assert downlink_pts(packets) == source_pts(target)
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_server_source_seek_restarts_reader_at_new_epoch(library_file):
    root, target = library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=180,
            ))
            await client.attach_media()
            await client.play()
            await collect_some(client, 3)

            expected = source_pts(target)
            target_pts = expected[len(expected) // 2]
            await client.seek(target_pts)
            packets = await collect(client)
            assert all(packet.epoch == 1 for packet in packets)
            assert packets[0].discontinuity
            assert packets[-1].eos
            assert downlink_pts(packets) == [pts for pts in expected if pts >= target_pts]
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
