"""Session-wide test setup."""
import faulthandler
import os
import sys

import pytest

# libmpv's mpv_create() returns NULL unless LC_NUMERIC is "C", and
# python-mpv then segfaults in mpv_set_option. QApplication's constructor
# adopts the environment's locale, so tests that build their own
# QApplication crashed under en_US.UTF-8 (CI's C.UTF-8 hid it). relay-desktop
# resets LC_NUMERIC after creating its QApplication (desktop_client/app.py).
os.environ["LC_NUMERIC"] = "C"


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # On Windows, LuaJIT (mpv's built-in scripts) raises and catches SEH
    # exception 0xe24c4a02, which faulthandler takes for a fatal error: it
    # dumps every thread's stack while those threads keep running. That
    # unsynchronized walk crashed desktop playback test runs with an access
    # violation (2 of 8; 0 of 16 with faulthandler off, 0 of 8 with mpv's
    # scripts off). Dump only the faulting thread there.
    if sys.platform != "win32" or not faulthandler.is_enabled():
        return
    from _pytest import faulthandler as plugin

    fd = config.stash.get(plugin.fault_handler_stderr_fd_key, None)
    faulthandler.enable(file=sys.__stderr__ if fd is None else fd, all_threads=False)


@pytest.fixture(autouse=True)
def _no_display_mapper_detection(monkeypatch):
    """A server started by a test offers no display mapping, whatever ffmpeg
    is on PATH: detection runs the GPU. Tests of it probe a mapper directly."""
    from relay_server.display_mapping import DisplayMapper

    monkeypatch.setattr(DisplayMapper, "detect", classmethod(lambda cls, ffmpeg=None: None))
