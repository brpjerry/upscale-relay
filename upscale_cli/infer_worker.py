"""Process-isolated inference.

The ORT TensorRT EP on this stack (Windows / Blackwell / TRT 10.9-10.13)
corrupts the process heap under sustained streaming — reproducibly crashing
*other* components (usually the libav decoder) seconds in, even single-
threaded. Isolating the session in a worker process contains the blast
radius: frames cross over shared memory, and a worker crash is a recoverable
error instead of a dead server.

Worker protocol (over stdin/stdout pipes, payloads in SharedMemory):
  parent -> worker: 8 bytes <u32 h><u32 w>   frame is in shm_in, rgb24 HWC
  worker -> parent: 8 bytes <u32 h2><u32 w2> result in shm_out, rgb24 HWC
  h == 0 means shutdown. Worker prints "READY <scale>" on stdout at startup
  (after the session + probe are up, i.e. after any engine build).
"""

from __future__ import annotations

import queue
import struct
import subprocess
import sys
import threading
from multiprocessing import shared_memory

import numpy as np

_HDR = struct.Struct("<II")

# Input cap = TRT profile max; output cap covers scale 4x.
_MAX_IN = (1440, 2560)
_MAX_IN_BYTES = _MAX_IN[0] * _MAX_IN[1] * 3
_MAX_OUT_BYTES = _MAX_IN_BYTES * 16  # 4x scale in both dims


def worker_main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--ep", default="tensorrt")
    parser.add_argument("--tile", default="none")
    parser.add_argument("--shm-in", required=True)
    parser.add_argument("--shm-out", required=True)
    args = parser.parse_args()

    from upscale_cli.infer import OnnxUpscaler

    tile = None if args.tile in ("none", "None") else (
        "auto" if args.tile == "auto" else int(args.tile))
    up = OnnxUpscaler(args.model, ep=args.ep, tile_size=tile)

    shm_in = shared_memory.SharedMemory(name=args.shm_in)
    shm_out = shared_memory.SharedMemory(name=args.shm_out)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    print(f"READY {up.scale_factor or 0}", flush=True)

    try:
        while True:
            hdr = stdin.read(_HDR.size)
            if len(hdr) < _HDR.size:
                return 0
            h, w = _HDR.unpack(hdr)
            if h == 0:
                return 0
            rgb = np.ndarray((h, w, 3), dtype=np.uint8, buffer=shm_in.buf)
            out = up._infer_with_fallback(rgb)
            oh, ow = out.shape[:2]
            dst = np.ndarray((oh, ow, 3), dtype=np.uint8, buffer=shm_out.buf)
            np.copyto(dst, out)  # single copy, no intermediate bytes object
            stdout.write(_HDR.pack(oh, ow))
            stdout.flush()
    finally:
        shm_in.close()
        shm_out.close()


class SubprocessUpscaler:
    """OnnxUpscaler-compatible facade running inference out-of-process.

    Restarts the worker once on crash/timeout; a second consecutive failure
    raises (surfaced as a session error, never a dead server).
    """

    FIRST_FRAME_TIMEOUT = 300.0  # cold TensorRT engine build
    FRAME_TIMEOUT = 60.0

    def __init__(self, model_path: str, ep: str = "tensorrt",
                 tile_size: int | str | None = None):
        from upscale_cli.manifest import ModelManifest

        self.model_path = model_path
        self.ep = ep
        self.tile_size = tile_size
        self.manifest = ModelManifest.load(model_path)
        self.scale_factor = self.manifest.scale_factor
        if self.scale_factor is None:
            raise ValueError(f"model {model_path} needs a manifest with scale_factor")
        self._lock = threading.Lock()
        self._shm_in = shared_memory.SharedMemory(create=True, size=_MAX_IN_BYTES)
        self._shm_out = shared_memory.SharedMemory(create=True, size=_MAX_OUT_BYTES)
        self._proc: subprocess.Popen | None = None
        self._first_frame_done = False
        self._start_worker()

    def _start_worker(self) -> None:
        tile = "none" if self.tile_size is None else str(self.tile_size)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "upscale_cli.infer_worker",
             "--model", self.model_path, "--ep", self.ep, "--tile", tile,
             "--shm-in", self._shm_in.name, "--shm-out", self._shm_out.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        )
        # ONE persistent reader thread per worker: a thread-per-frame read
        # pattern churned ~30 threads/s at streaming rate.
        self._reply_q: queue.Queue = queue.Queue()
        proc = self._proc

        def _reader():
            stdout = proc.stdout
            line = stdout.readline()  # READY line
            self._reply_q.put(line if line else None)
            while True:
                hdr = stdout.read(_HDR.size)
                if not hdr or len(hdr) < _HDR.size:
                    self._reply_q.put(None)  # worker died / EOF
                    return
                self._reply_q.put(hdr)

        self._reader = threading.Thread(target=_reader, daemon=True,
                                        name="infer-worker-reader")
        self._reader.start()

        # Wait for READY (includes any TensorRT engine build).
        try:
            line = self._reply_q.get(timeout=self.FIRST_FRAME_TIMEOUT)
        except queue.Empty:
            line = None
        if not line or not line.startswith(b"READY"):
            self._kill()
            raise RuntimeError(f"inference worker failed to start: {line!r}")

    def _read_exact(self, n: int, timeout: float) -> bytes | None:
        """Next framed reply from the persistent reader (n is fixed = header)."""
        try:
            reply = self._reply_q.get(timeout=timeout)
        except queue.Empty:
            return None
        if reply is None or len(reply) < n:
            return None
        return reply

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def _roundtrip(self, rgb: np.ndarray, timeout: float) -> np.ndarray | None:
        h, w = rgb.shape[:2]
        dst = np.ndarray((h, w, 3), dtype=np.uint8, buffer=self._shm_in.buf)
        np.copyto(dst, rgb)  # handles non-contiguous input, no bytes object
        try:
            self._proc.stdin.write(_HDR.pack(h, w))
            self._proc.stdin.flush()
        except OSError:
            return None
        reply = self._read_exact(_HDR.size, timeout)
        if reply is None:
            return None
        oh, ow = _HDR.unpack(reply)
        out = np.ndarray((oh, ow, 3), dtype=np.uint8,
                         buffer=self._shm_out.buf[: oh * ow * 3]).copy()
        return out

    # -- OnnxUpscaler-compatible surface --------------------------------------

    def _infer_with_fallback(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        if h > _MAX_IN[0] or w > _MAX_IN[1]:
            raise ValueError(f"frame {w}x{h} exceeds worker input cap {_MAX_IN[1]}x{_MAX_IN[0]}")
        with self._lock:
            timeout = self.FRAME_TIMEOUT if self._first_frame_done else self.FIRST_FRAME_TIMEOUT
            out = self._roundtrip(rgb, timeout)
            if out is None:
                # Worker died (the contained TRT crash): restart once, retry.
                print("inference worker died; restarting", file=sys.stderr, flush=True)
                self._kill()
                self._start_worker()
                out = self._roundtrip(rgb, self.FIRST_FRAME_TIMEOUT)
                if out is None:
                    self._kill()
                    raise RuntimeError("inference worker crashed twice on one frame")
            self._first_frame_done = True
            return out

    infer_array = _infer_with_fallback

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.write(_HDR.pack(0, 0))
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._kill()
                self._proc = None
            self._shm_in.close()
            self._shm_in.unlink()
            self._shm_out.close()
            self._shm_out.unlink()


if __name__ == "__main__":
    raise SystemExit(worker_main())
