"""Session-wide test setup."""
import faulthandler
import sys

import pytest


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
