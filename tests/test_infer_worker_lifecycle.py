from multiprocessing import shared_memory

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
