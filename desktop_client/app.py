"""Desktop client entry point.

    python -m desktop_client.app
"""

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from .options import DesktopOptions


def parse_args(argv: list[str] | None = None) -> tuple[DesktopOptions, list[str]]:
    parser = argparse.ArgumentParser(prog="relay-desktop")
    parser.add_argument("--debug", action="store_true", help="enable faulthandler crash dumps")
    parser.add_argument("--trace", action="store_true", help="trace relay/mpv packet feeding")
    parser.add_argument("--mpv-osc", action="store_true", help="enable mpv's OSC overlay")
    parser.add_argument("--no-hwdec", action="store_true", help="force software video decoding")
    parser.add_argument("--mpv-scripts", action="store_true", help="load the scripts in mpv's scripts folder")
    parser.add_argument("--headless", action="store_true", help="use null mpv audio/video outputs")
    parser.add_argument(
        "--settings-scope", metavar="NAME",
        help="use an isolated QSettings application name (primarily for tests)",
    )
    parser.add_argument("--input-conf", type=Path, help="key bindings file instead of mpv's input.conf")
    parser.add_argument("--log-root", type=Path, help="override client log directory")
    parser.add_argument("--no-discovery", action="store_true", help="disable nearby server discovery")
    args, qt_args = parser.parse_known_args(argv)
    return DesktopOptions(
        debug=args.debug,
        trace=args.trace,
        mpv_osc=args.mpv_osc,
        no_hwdec=args.no_hwdec,
        mpv_scripts=args.mpv_scripts,
        headless=args.headless,
        settings_scope=args.settings_scope,
        input_conf_path=args.input_conf,
        log_root=args.log_root,
        discovery=False if args.no_discovery else None,
    ), qt_args


def main() -> None:
    # Opt-in only: mpv's OSC runs on LuaJIT, whose internal (caught) SEH
    # exceptions make faulthandler dump all threads on every stream reload —
    # pure noise that looks like crashes.
    options, qt_args = parse_args(sys.argv[1:])
    if options.debug:
        import faulthandler

        # On Windows faulthandler also fires on LuaJIT's caught SEH exceptions
        # (mpv's built-in scripts), and dumping every thread while they run
        # crashed the player itself. Dump only the faulting thread there.
        faulthandler.enable(all_threads=sys.platform != "win32")

    # Preserve unknown arguments for Qt itself (e.g. -platform), while
    # consuming the relay-specific flags above.
    app = QApplication([sys.argv[0], *qt_args])
    # QApplication sets the process locale from the environment; libmpv
    # aborts ("Non-C locale detected") unless LC_NUMERIC is "C".
    import locale

    locale.setlocale(locale.LC_NUMERIC, "C")
    from .main_window import MainWindow

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow(options=options)
    window.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
