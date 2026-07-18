"""Desktop client entry point.

    python -m desktop_client.app
"""

from __future__ import annotations

import asyncio
import argparse
import sys

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from .options import DesktopOptions
from .theme import apply_system_theme


def parse_args(argv: list[str] | None = None) -> tuple[DesktopOptions, list[str]]:
    parser = argparse.ArgumentParser(prog="relay-desktop")
    parser.add_argument("--debug", action="store_true", help="enable faulthandler crash dumps")
    parser.add_argument("--trace", action="store_true", help="trace relay/mpv packet feeding")
    parser.add_argument("--mpv-osc", action="store_true", help="enable mpv's OSC overlay")
    parser.add_argument("--no-hwdec", action="store_true", help="force software video decoding")
    parser.add_argument("--mpv-scripts", action="store_true", help="load user mpv scripts")
    parser.add_argument("--headless", action="store_true", help="use null mpv audio/video outputs")
    parser.add_argument(
        "--settings-scope", metavar="NAME",
        help="use an isolated QSettings application name (primarily for tests)",
    )
    args, qt_args = parser.parse_known_args(argv)
    return DesktopOptions(
        debug=args.debug,
        trace=args.trace,
        mpv_osc=args.mpv_osc,
        no_hwdec=args.no_hwdec,
        mpv_scripts=args.mpv_scripts,
        headless=args.headless,
        settings_scope=args.settings_scope,
    ), qt_args


def main() -> None:
    # Opt-in only: mpv's OSC runs on LuaJIT, whose internal (caught) SEH
    # exceptions make faulthandler dump all threads on every stream reload —
    # pure noise that looks like crashes.
    options, qt_args = parse_args(sys.argv[1:])
    if options.debug:
        import faulthandler

        faulthandler.enable()

    # Preserve unknown arguments for Qt itself (e.g. -platform), while
    # consuming the relay-specific flags above.
    app = QApplication([sys.argv[0], *qt_args])
    # QApplication sets the process locale from the environment; libmpv
    # aborts ("Non-C locale detected") unless LC_NUMERIC is "C".
    import locale

    locale.setlocale(locale.LC_NUMERIC, "C")
    apply_system_theme(app)
    from .main_window import MainWindow

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow(options=options)
    window.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
