import asyncio
import gc

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from qt_helpers import playback_loop


def test_closed_playback_loop_retires_pending_native_timers(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with playback_loop(app) as loop:
        timer = loop._timer
        loop.call_later(60, pytest.fail, "closed loop delivered a stale callback")
        loop.run_until_complete(asyncio.sleep(0))
    assert not isValid(timer)
    del loop

    # Server integration tests collect cycles on worker threads before the next
    # GUI test. There must be no retired Qt timers left for that GC to destroy.
    asyncio.run(asyncio.to_thread(gc.collect))
    with playback_loop(app) as loop:
        loop.run_until_complete(asyncio.sleep(0.01))
