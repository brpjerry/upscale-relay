"""Frozen-build (PyInstaller) entry point for the relay server.

The frozen Windows binary ships with the DirectML/CPU execution providers.
The TensorRT path launches a Python worker subprocess and therefore still
requires a source install in a CUDA virtualenv.
"""

import multiprocessing

from upscale_cli.infer_worker import maybe_run_frozen_worker

if __name__ == "__main__":
    multiprocessing.freeze_support()
    worker_result = maybe_run_frozen_worker()
    if worker_result is not None:
        raise SystemExit(worker_result)
    from relay_server.server import main

    main()
