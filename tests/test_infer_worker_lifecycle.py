from multiprocessing import shared_memory
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from upscale_cli import infer_worker


@pytest.mark.parametrize("failure", ["second_allocation", "startup"])
def test_failed_constructor_releases_every_shared_memory_allocation(tmp_path, monkeypatch, failure):
    original = shared_memory.SharedMemory
    names = []

    def allocate(**kwargs):
        if failure == "second_allocation" and names:
            raise OSError("allocation failed")
        block = original(**kwargs)
        names.append(block.name)
        return block

    def fail_start(_self):
        raise OSError("startup failed")

    monkeypatch.setattr(infer_worker, "_MAX_IN_BYTES", 48)
    monkeypatch.setattr(infer_worker, "_MAX_OUT_BYTES", 768)
    monkeypatch.setattr(shared_memory, "SharedMemory", allocate)
    monkeypatch.setattr(infer_worker.SubprocessUpscaler, "_start_worker", fail_start)
    try:
        with pytest.raises(OSError, match="failed"):
            infer_worker.SubprocessUpscaler(str(tmp_path / "model2x.onnx"))
        assert len(names) == (1 if failure == "second_allocation" else 2)
        for name in names:
            with pytest.raises(FileNotFoundError):
                original(name=name)
    finally:
        # Keep the test's failure path from leaving OS resources behind.
        for name in names:
            try:
                block = original(name=name)
            except FileNotFoundError:
                continue
            block.close()
            block.unlink()


def test_delayed_reply_stays_in_its_worker_generation(monkeypatch):
    eof = threading.Event()

    class Pipe:
        def readline(self):
            return b"READY 2 CPUExecutionProvider\n"

        def read(self, size):
            assert eof.wait(2)
            return b""

    worker = infer_worker.SubprocessUpscaler.__new__(infer_worker.SubprocessUpscaler)
    worker.tile_size = None
    worker.model_path = "unused"
    worker.ep = "cpu"
    worker._shm_in = worker._shm_out = SimpleNamespace(name="unused")
    monkeypatch.setattr(infer_worker.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(stdout=Pipe()))
    worker._start_worker()
    old_queue = worker._reply_q
    worker._reply_q = queue.Queue()
    eof.set()
    worker._reader.join(timeout=2)
    assert not worker._reader.is_alive()
    assert old_queue.get_nowait() is None
    assert worker._reply_q.empty()


def test_worker_restart_reaps_old_process_and_closes_its_pipes(tmp_path, monkeypatch):
    monkeypatch.setattr(infer_worker, "_MAX_IN_BYTES", 48)
    monkeypatch.setattr(infer_worker, "_MAX_OUT_BYTES", 768)
    script = "import time; print('READY 2 CPUExecutionProvider', flush=True); time.sleep(60)"
    monkeypatch.setattr(infer_worker, "build_worker_command", lambda *args: [sys.executable, "-c", script])
    worker = infer_worker.SubprocessUpscaler(str(tmp_path / "model2x.onnx"), ep="cpu")
    try:
        old_process, old_reader = worker._proc, worker._reader
        worker._kill()
        assert old_process.poll() is not None
        assert not old_reader.is_alive()
        assert old_process.stdin.closed and old_process.stdout.closed
        worker._start_worker()
        assert worker.active_provider == "CPUExecutionProvider"
        assert worker._reply_q.empty()
    finally:
        worker._kill()
        worker.close()
        worker.close()  # cleanup is safe after failed or repeated teardown
