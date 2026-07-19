"""Frozen-build (PyInstaller) entry point for the relay server.

The small Windows binary installs the pinned NVIDIA runtime into a versioned
per-user directory on first launch.  It also hosts the isolated TensorRT worker
by re-entering this executable through an internal dispatch.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    # This import intentionally precedes infer_worker: that module imports
    # numpy, while the external ORT/TensorRT runtime must be activated first.
    from relay_server.runtime_bootstrap import (
        activate_runtime,
        ensure_runtime_console,
        maybe_run_runtime_installer,
    )

    installer_result = maybe_run_runtime_installer()
    if installer_result is not None:
        raise SystemExit(installer_result)

    activate_runtime()
    from upscale_cli.infer_worker import maybe_run_frozen_worker

    worker_result = maybe_run_frozen_worker()
    if worker_result is not None:
        raise SystemExit(worker_result)

    # Help must remain instant/offline. Ordinary startup prepares the runtime
    # before the server can accept a model-backed session.
    if not any(arg in ("-h", "--help", "--check") for arg in sys.argv[1:]):
        if not ensure_runtime_console():
            raise SystemExit(1)
    from relay_server.server import main

    main()
