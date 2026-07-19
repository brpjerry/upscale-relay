"""Frozen-build (PyInstaller) entry point for the tray-GUI relay server.

Windowed sibling of ``server_entry.py``: double-clicking the exe starts the
server from its saved configuration and shows a notification-area icon with a
configuration pane. The small binary installs TensorRT/CUDA into a versioned
per-user runtime on first launch and hosts the isolated TensorRT worker through
its internal dispatch.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from relay_server.runtime_bootstrap import (
        activate_runtime,
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
    from relay_server.tray import main

    main()
