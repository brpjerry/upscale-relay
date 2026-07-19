"""Frozen-build (PyInstaller) entry point for the tray-GUI relay server.

Windowed sibling of ``server_entry.py``: double-clicking the exe starts the
server from its saved configuration and shows a notification-area icon with a
configuration pane. The frozen binary ships the DirectML/CPU execution
providers; the TensorRT path still needs a CUDA source install.
"""

import multiprocessing

from upscale_cli.infer_worker import maybe_run_frozen_worker

if __name__ == "__main__":
    multiprocessing.freeze_support()
    worker_result = maybe_run_frozen_worker()
    if worker_result is not None:
        raise SystemExit(worker_result)
    from relay_server.tray import main

    main()
