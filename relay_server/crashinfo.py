"""Native crash attribution (Windows).

faulthandler prints Python frames, but the intermittent server AV originates
in native code — this vectored exception handler prints the faulting module
and offset, which is the datum that names the guilty DLL.

Best-effort by design: the handler runs on the crashing thread and needs the
GIL for the ctypes callback; if that can't be acquired the process just dies
as before, losing nothing.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_EXCEPTION_ACCESS_VIOLATION = 0xC0000005
_EXCEPTION_CONTINUE_SEARCH = 0


class _EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", wintypes.DWORD),
        ("ExceptionFlags", wintypes.DWORD),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wintypes.DWORD),
        ("ExceptionInformation", ctypes.c_size_t * 15),
    ]


class _EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [
        ("ExceptionRecord", ctypes.POINTER(_EXCEPTION_RECORD)),
        ("ContextRecord", ctypes.c_void_p),
    ]


_HANDLER_TYPE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
_handler_ref = None  # keep the callback alive


def install() -> None:
    global _handler_ref
    if _handler_ref is not None or sys.platform != "win32":
        return
    kernel32 = ctypes.windll.kernel32

    @_HANDLER_TYPE
    def _veh(ptrs_addr):
        try:
            ptrs = ctypes.cast(ptrs_addr, ctypes.POINTER(_EXCEPTION_POINTERS)).contents
            rec = ptrs.ExceptionRecord.contents
            if rec.ExceptionCode != _EXCEPTION_ACCESS_VIOLATION:
                return _EXCEPTION_CONTINUE_SEARCH
            addr = rec.ExceptionAddress or 0
            hmod = wintypes.HMODULE(0)
            # GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | UNCHANGED_REFCOUNT
            kernel32.GetModuleHandleExW(0x4 | 0x2, ctypes.c_wchar_p(addr), ctypes.byref(hmod))
            buf = ctypes.create_unicode_buffer(512)
            if hmod.value:
                kernel32.GetModuleFileNameW(hmod, buf, 512)
            mode = rec.ExceptionInformation[0] if rec.NumberParameters >= 2 else 99
            target = rec.ExceptionInformation[1] if rec.NumberParameters >= 2 else 0
            kind = {0: "read", 1: "write", 8: "exec"}.get(mode, "?")
            base = hmod.value or 0
            sys.stderr.write(
                f"\n[crashinfo] ACCESS VIOLATION at 0x{addr:016x} "
                f"({buf.value or '<no module>'} +0x{addr - base:x}) "
                f"while {kind} of 0x{target:016x}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
        return _EXCEPTION_CONTINUE_SEARCH

    _handler_ref = _veh
    kernel32.AddVectoredExceptionHandler(1, _veh)
