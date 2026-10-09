"""capabilities.server_id: a stable, persisted per-install identity."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ports import free_port_pair

from relay_client_core import RelayClient
from relay_server.server import RelayServer
from relay_server.server_id import FILE_NAME, VALID_SERVER_ID, load_or_create_server_id

ROOT = Path(__file__).resolve().parents[1]


def test_id_is_created_once_and_then_reused(tmp_path):
    state = tmp_path / "state"  # created on first use
    first = load_or_create_server_id(state)
    assert first is not None and VALID_SERVER_ID.fullmatch(first)
    assert load_or_create_server_id(state) == first
    assert (state / FILE_NAME).read_text(encoding="ascii").strip() == first


def test_damaged_id_file_is_replaced(tmp_path):
    (tmp_path / FILE_NAME).write_text("not a valid id!\n", encoding="ascii")
    replaced = load_or_create_server_id(tmp_path)
    assert replaced is not None and VALID_SERVER_ID.fullmatch(replaced)
    assert load_or_create_server_id(tmp_path) == replaced


def test_deleting_the_file_makes_a_new_id(tmp_path):
    first = load_or_create_server_id(tmp_path)
    (tmp_path / FILE_NAME).unlink()
    assert load_or_create_server_id(tmp_path) not in (None, first)


def test_unwritable_state_directory_means_no_id(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("a file where the directory should be")
    assert load_or_create_server_id(blocker / "state") is None


def test_lock_dir_override_locates_the_id(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSCALE_RELAY_LOCK_DIR", str(tmp_path))
    created = load_or_create_server_id()
    assert (tmp_path / FILE_NAME).read_text(encoding="ascii").strip() == created


def _capabilities(**server_kwargs) -> dict:
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), **server_kwargs)
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            return await client.connect()
        finally:
            await client.teardown()
            await server.stop()

    return asyncio.run(scenario())


def test_capabilities_advertise_the_server_id():
    assert _capabilities(server_id="0123abcd")["server_id"] == "0123abcd"


def test_capabilities_omit_an_unknown_server_id():
    assert "server_id" not in _capabilities()
