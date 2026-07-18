"""Frozen-build (PyInstaller) entry point for the relay server.

The frozen Windows binary ships with the DirectML/CPU execution providers.
The TensorRT path launches a Python worker subprocess and therefore still
requires a source install in a CUDA virtualenv.
"""

import multiprocessing

from relay_server.server import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
