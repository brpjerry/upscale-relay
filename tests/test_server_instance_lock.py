"""The server refuses to start while another server process holds the lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from relay_server import instance_lock
from relay_server.instance_lock import AlreadyRunningError, acquire_instance_lock

_HOLDER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from relay_server.instance_lock import acquire_instance_lock
    acquire_instance_lock(Path(sys.argv[1]))
    print("locked", flush=True)
    sys.stdin.read()
""")


@pytest.fixture
def holder(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def _no_held_lock():
    yield
    if instance_lock._held is not None:
        instance_lock._held.release()


def test_second_process_is_refused_with_holder_pid(tmp_path, holder):
    with pytest.raises(AlreadyRunningError) as caught:
        acquire_instance_lock(tmp_path)
    assert caught.value.pid == holder.pid
    assert f"PID {holder.pid}" in str(caught.value)


def test_killed_holder_leaves_no_stale_lock(tmp_path, holder):
    holder.kill()
    holder.wait(timeout=10)
    lock = acquire_instance_lock(tmp_path)
    assert (tmp_path / "relay-server.lock").exists()
    lock.release()


def test_reacquire_in_same_process_is_a_no_op(tmp_path):
    first = acquire_instance_lock(tmp_path)
    assert acquire_instance_lock(tmp_path) is first
    first.release()
    second = acquire_instance_lock(tmp_path)
    assert second is not first
    second.release()


def test_relay_server_cli_exits_nonzero_when_lock_held(tmp_path, holder):
    env = dict(os.environ, UPSCALE_RELAY_LOCK_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "relay_server.server", "--no-mdns", "--port", "1"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "already running" in result.stderr
    assert f"PID {holder.pid}" in result.stderr
