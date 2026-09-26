"""Bounded optional client logging. Producers never perform filesystem I/O."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import traceback


def redact(text: str) -> str:
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[media URL]", text)
    text = re.sub(
        r"(?i)([\w-]*(?:token|authorization)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+[^\s,;]+|[^\s,;\"'}]+)",
        r"\1[redacted]", text,
    )
    return re.sub(r"(?i)(\b(?:bearer|basic)\s+)[^\s,;\"'}]+", r"\1[redacted]", text)


def _safe(value, depth=0):
    if isinstance(value, dict) and depth < 4:
        return {str(key): ("[redacted]" if re.search("token|authorization", str(key), re.I)
                          else _safe(item, depth + 1)) for key, item in list(value.items())[:128]}
    if isinstance(value, (tuple, list)) and depth < 4:
        return [_safe(item, depth + 1) for item in value[:128]]
    if value is None or type(value) in (bool, int, float):
        return value
    return redact(str(value)[:16384])


class ClientLog:
    def __init__(self, root: Path, *, max_bytes=2 * 1024 * 1024, retain=10, capacity=512):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.retain = retain
        self.queue = queue.Queue(maxsize=capacity)
        self.enabled = False
        self.path: Path | None = None
        self.error: str | None = None
        self._thread = None
        self._closed = threading.Event()
        self._sequence = 0
        self._hooks = None

    def enable(self, enabled):
        if self.enabled and not enabled:
            self.record("logging", enabled=False)
        self.enabled = bool(enabled)
        if enabled and (self._thread is None or not self._thread.is_alive()):
            self.error = None
            self._thread = threading.Thread(target=self._write, name="client-log", daemon=True)
            self._thread.start()
        self.record("logging", enabled=enabled)

    def record(self, event: str, **fields):
        if not self.enabled or self._closed.is_set():
            return
        try:
            payload = _safe({"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields})
            line = json.dumps(payload, ensure_ascii=False)
            if len(line) > 8192:
                # Keep each record valid JSON even when a native message is huge.
                summary = line[:4096]
                while True:
                    line = json.dumps({"time": payload["time"], "event": payload["event"],
                                       "truncated": True, "detail": summary}, ensure_ascii=False)
                    if len(line) <= 8192:
                        break
                    summary = summary[:len(summary) // 2]
            self.queue.put_nowait(line + "\n")
        except Exception:
            # Queue saturation or even a pathological exception __str__ must
            # never make optional diagnostics affect playback.
            pass

    def _write(self):
        stream = None
        size = 0
        try:
            while not self._closed.is_set() or not self.queue.empty():
                try:
                    line = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                data = line.encode("utf-8")
                if stream is None or size + len(data) > self.max_bytes:
                    if stream:
                        stream.close()
                    self.root.mkdir(parents=True, exist_ok=True)
                    self._sequence += 1
                    self.path = self.root / (f"upscale-relay-client-{datetime.now():%Y%m%d-%H%M%S-%f}"
                                             f"-{os.getpid()}-{self._sequence}.log")
                    stream = self.path.open("xb")
                    size = 0
                    files = sorted(self.root.glob("upscale-relay-client-*.log"), key=lambda p: p.name)
                    for old in files[:-self.retain]:
                        old.unlink(missing_ok=True)
                stream.write(data)
                stream.flush()
                size += len(data)
        except Exception as err:
            self.error = redact(str(err))
            self.enabled = False
        finally:
            if stream:
                stream.close()

    def install_hooks(self, loop):
        old_sys, old_thread, old_async = sys.excepthook, threading.excepthook, loop.get_exception_handler()
        def python_error(kind, value, tb):
            self.record("python_exception", detail="".join(traceback.format_exception(kind, value, tb)))
            old_sys(kind, value, tb)
        def thread_error(args):
            self.record("thread_exception", detail="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
            old_thread(args)
        def async_error(active_loop, context):
            self.record("async_exception", message=context.get("message"), error=context.get("exception"))
            (old_async or type(active_loop).default_exception_handler)(active_loop, context)
        sys.excepthook, threading.excepthook = python_error, thread_error
        loop.set_exception_handler(async_error)
        self._hooks = (loop, old_sys, old_thread, old_async, python_error, thread_error, async_error)

    def close(self):
        self.record("application_close")
        self._closed.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._hooks:
            loop, old_sys, old_thread, old_async, ours_sys, ours_thread, ours_async = self._hooks
            if sys.excepthook is ours_sys:
                sys.excepthook = old_sys
            if threading.excepthook is ours_thread:
                threading.excepthook = old_thread
            if loop.get_exception_handler() is ours_async:
                loop.set_exception_handler(old_async)
            self._hooks = None
