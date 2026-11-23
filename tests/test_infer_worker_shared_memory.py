"""The inference worker must leave the parent's shared memory in place."""

import subprocess
import sys
import time
from multiprocessing import shared_memory


# Bugs caught: 1, False positives: 0
def test_worker_attach_survives_worker_exit():
    # On Linux a plain attach handed the block to the worker's resource
    # tracker, which unlinked it when the worker exited, so a replacement
    # worker (same names) could not attach after a crash.
    block = shared_memory.SharedMemory(create=True, size=4096)
    try:
        child = (
            "from upscale_cli.infer_worker import attach_shared_memory\n"
            f"attach_shared_memory({block.name!r}).close()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", child], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "leaked shared_memory" not in result.stderr
        # The tracker cleans up from its own process, just after the child.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            shared_memory.SharedMemory(name=block.name).close()
            time.sleep(0.05)
    finally:
        block.close()
        block.unlink()
