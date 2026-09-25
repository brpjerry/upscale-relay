"""Deterministic cleanup for tests that enter Qt's native event loop."""

from contextlib import contextmanager


@contextmanager
def playback_loop(app):
    from PySide6.QtCore import QCoreApplication, QEvent
    from qasync import QEventLoop

    loop = QEventLoop(app)
    try:
        with loop:
            yield loop
    finally:
        # qasync 0.28 close() only marks its timer stopped: outstanding native
        # timers survive until another Qt loop runs. If a server worker triggers
        # cyclic GC first, that QObject can die off-thread and leave Qt with a
        # dangling timer receiver (a reproducible Linux CI segfault). Retire it
        # here on the GUI thread, while we still own the closed loop. Deliver
        # only this deferred deletion, never a nested processEvents in a task.
        loop._timer.deleteLater()
        QCoreApplication.sendPostedEvents(loop._timer, QEvent.DeferredDelete)
