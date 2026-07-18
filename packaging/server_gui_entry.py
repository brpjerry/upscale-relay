"""Frozen-build (PyInstaller) entry point for the tray-GUI relay server.

Windowed sibling of ``server_entry.py``: double-clicking the exe starts the
server from its saved configuration and shows a notification-area icon with a
configuration pane. The frozen binary ships the DirectML/CPU execution
providers; the TensorRT path still needs a CUDA source install.
"""

import multiprocessing

from relay_server.tray import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
