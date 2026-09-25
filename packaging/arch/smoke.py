"""Run with python -I after pacman installs the package, outside the checkout."""

import asyncio
import importlib.util
import locale
import os
from pathlib import Path
import tempfile


def main():
    # Catch accidental server/inference packaging and source-checkout imports.
    for module in ("relay_server", "upscale_cli", "onnx", "onnxruntime", "tensorrt"):
        assert importlib.util.find_spec(module) is None, module
    for module in ("desktop_client", "relay_client_core", "relay_media", "relay_protocol"):
        spec = importlib.util.find_spec(module)
        assert spec is not None and Path(spec.origin).is_relative_to("/usr/lib"), module

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="relay-package-smoke-") as state:
        os.environ["XDG_CONFIG_HOME"] = state
        os.environ["XDG_CACHE_HOME"] = state
        from PySide6.QtCore import QLibraryInfo
        from PySide6.QtWidgets import QApplication
        from qasync import QEventLoop
        from desktop_client.main_window import MainWindow
        from desktop_client.options import DesktopOptions

        plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
        assert list((plugins / "platforms").glob("*wayland*.so")), "Missing Qt Wayland plugin"
        app = QApplication([])
        locale.setlocale(locale.LC_NUMERIC, "C")
        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)
        window = MainWindow(DesktopOptions(headless=True, settings_scope="pacman-smoke"))
        try:
            with loop:
                loop.run_until_complete(asyncio.sleep(0.1))
            assert window.player.mpv is not None
        finally:
            window.close()
            window.player.mpv.terminate()
    print("Installed client, libmpv, Qt and Wayland plugin smoke passed")


if __name__ == "__main__":
    main()
