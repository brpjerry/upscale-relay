"""Relay server entry point: aiohttp control/status + TCP media listener.

    python -m relay_server.server --models-dir models --port 8590

Control WebSocket: ws://host:8590/control
Status:            http://host:8590/status
Media (TCP):       host:8591 (control port + 1)
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hmac
import json
import logging
import math
import re
import signal
import sys
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Iterable

from aiohttp import WSCloseCode, WSMsgType, web, web_fileresponse

from relay_protocol import (
    CLOSE_SESSION_TAKEN_OVER,
    DIR_DOWNLINK,
    DIR_UPLINK,
    HANDSHAKE_LEN,
    PROTOCOL_VERSION,
    encode_packet,
    parse_handshake,
    read_packet,
)
from upscale_cli.encode import (
    DEFAULT_LOSSLESS_HEVC_PROFILE,
    LOSSLESS_HEVC_PROFILES,
    QUALITY_OPTION_IDS,
    QUALITY_OPTIONS,
)
from upscale_cli.fit import DEFAULT_RESIZE_ALGORITHM, RESIZE_ALGORITHMS
from upscale_cli.manifest import ModelManifest

from .display_mapping import DisplayMapper
from .mdns import MdnsAdvertiser
from .session import Session, State
from .library import SORT_KEYS as LIBRARY_SORT_KEYS, LibraryPathError, MediaLibrary

log = logging.getLogger("relay.server")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

if sys.platform == "win32":
    # `web.FileResponse` streams with the event loop's native sendfile, which on
    # Windows is TransmitFile. That call takes at most 2,147,483,646 bytes, but
    # asyncio clamps its block to 0xffff_ffff (CPython proactor_events), so any
    # request whose remaining length exceeds 2 GiB raises WinError 87 — and
    # aiohttp falls back to its own read loop only on NotImplementedError, never
    # OSError. The error therefore lands *after* the response headers are on the
    # wire: clients see a correct Content-Length and then a closed connection
    # with zero bytes. mpv opens external media with `Range: bytes=0-`, so every
    # remux over 2 GiB served the Android client no audio and no subtitles.
    # Bounded ranges under the limit worked, which is what made it look like a
    # client-side track-selection bug. NOSENDFILE is read per call, so setting
    # it here does not depend on import order.
    web_fileresponse.NOSENDFILE = True


def discover_models(models_dir: str) -> dict[str, dict]:
    """Return model metadata, generating a default manifest when absent."""
    out = {}
    for path in sorted(Path(models_dir).glob("*.onnx")):
        try:
            manifest = ModelManifest.load(path)
        except (OSError, ValueError):
            continue
        out[path.stem] = {"path": str(path), "scale_factor": manifest.scale_factor}
    return out


# Control WebSocket ping interval. aiohttp waits half of it for the pong, so a
# dead control connection is noticed within 1.5x this. Clients size their
# GET /status polling from it (docs/PROTOCOL.md 1.1: this plus
# PIPELINE_CLOSE_TIMEOUT_S is the documented 45 s worst case, and clients poll
# for 60 s); tests/test_server_status_contract.py holds the numbers together.
CONTROL_HEARTBEAT_S = 20.0
# How long a session whose client asked for resume (open_session.resume,
# PROTOCOL §1.2) is held after its control connection ends, for that client
# to reconnect and take it over. Production holds at the watermark meanwhile,
# so a held session costs GPU memory and its encoder, not GPU time. With the
# heartbeat and the pipeline close, a client that never comes back has its
# session released within CONTROL_HEARTBEAT_S x 1.5 + this + 15 s.
SESSION_RESUME_GRACE_S = 120.0


class RelayServer:
    def __init__(self, models_dir: str, port: int, ep: str = "auto",
                 stats_interval: float | None = None,
                 library_root: str | None = None,
                 resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM,
                 lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE,
                 mdns: bool = False,
                 seek_discard_max_s: float | None = None,
                 library_roots: Iterable[str | Path] | None = None,
                 server_id: str | None = None,
                 display_mapper: DisplayMapper | None = None,
                 resume_grace_s: float = SESSION_RESUME_GRACE_S):
        self.port = port
        # 0 turns resume off: capabilities.session_resume is false.
        if not (0 <= resume_grace_s and math.isfinite(resume_grace_s)):
            raise ValueError(f"resume_grace_s must be finite and >= 0: {resume_grace_s!r}")
        self.resume_grace_s = float(resume_grace_s)
        # Sessions held for a resume, by id: each task closes its session
        # when the grace period ends without one.
        self._held: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        # Applies Dolby Vision and HDR10+ for a client's display (PROTOCOL
        # §3.6); None: capabilities.display_mapping is false.
        self.display_mapper = display_mapper
        # Persistent install identity (relay_server.server_id); None omits
        # capabilities.server_id and clients key by host:port instead.
        self.server_id = server_id
        self.media_port = port + 1
        self.ep = ep
        self.stats_interval = stats_interval  # seconds; None = no periodic stats
        self.resize_algorithm = resize_algorithm
        self.lossless_hevc_profile = lossless_hevc_profile
        self.seek_discard_max_s = seek_discard_max_s
        self.models_info = discover_models(models_dir)
        self.models = {name: info["path"] for name, info in self.models_info.items()}
        if library_root and library_roots is not None:
            raise ValueError("specify library_root or library_roots, not both")
        configured_roots = library_roots if library_roots is not None else library_root
        self.library = MediaLibrary(configured_roots) if configured_roots else None
        self.sessions: dict[str, Session] = {}  # keyed by media tokens AND id
        self.native_teardown_error: dict | None = None
        # Optional GUI hook (kept Qt-free): called with a short human-readable
        # line when a not-yet-seen IP connects or a session starts playing.
        self.event_callback: Callable[[str], None] | None = None
        self._seen_ips: set[str] = set()
        self.app = web.Application()
        self.app.router.add_get("/control", self.handle_control)
        self.app.router.add_get("/status", self.handle_status)
        if self.library is not None:
            self.app.router.add_get("/library", self.handle_library)
            self.app.router.add_get("/media/{path:.*}", self.handle_media_file)
            self.app.router.add_get("/attachments/{digest}", self.handle_attachment)
        self._stats_task = None
        self._media_server = None
        self._runner = None
        self._stop_task = None
        self._media_handlers: dict[asyncio.Task, asyncio.StreamWriter] = {}
        # Every open control WebSocket, including those with no Session yet
        # (a client browsing the library). stop() closes them itself: aiohttp
        # otherwise waits out its shutdown timeout for each idle handler.
        self._control_sockets: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()
        self._mdns = MdnsAdvertiser(
            port=self.port,
            media_port=self.media_port,
            server_name="upscale-relay",
            protocol_version=PROTOCOL_VERSION,
        ) if mdns else None

    # -- periodic stats (--verbose) ----------------------------------------------

    @staticmethod
    def _log_session_stats(session: Session, *, final: bool = False) -> None:
        """Persist live performance fields without affecting session cleanup."""
        p = session.pipeline
        if p is None:
            return
        try:
            depths = p.queue_depths()
            stage = p.stats.stage_report()
            infer_ms = stage.get("infer")
            infer_fps = f"{1000.0 / infer_ms:5.1f}" if infer_ms else "  n/a"
            stage_str = " ".join(f"{k}={v}ms" for k, v in stage.items()) or "stages=n/a"
            cached = depths["decoded"] + depths["upscaled"] + session.down_q.qsize()
            stats_log = logging.getLogger("relay.stats")
            stats_log.info(
                "session %s %s%s epoch=%d source=%s | pipeline %5.1f fps | "
                "onnx %s fps | %s | output=%dx%d codec=%s encoder=%s tier=%s | "
                "cached frames: in-flight=%d (dec=%d up=%d down=%d) uplink-pkts=%d | "
                "client buffer %5d ms (est %5d, target %d) | %s | frames %d in / %d out",
                session.id[:6], session.state.value, " FINAL" if final else "",
                session.epoch, session.source_kind,
                p.stats.fps, infer_fps, stage_str,
                p.out_w, p.out_h, p.downlink_codec, p.encoder_name, p.quality_tier,
                cached, depths["decoded"], depths["upscaled"],
                session.down_q.qsize(), depths["in"],
                p.client_buffered_ms, p.buffered_ms_now(), p.buffer_target_ms,
                "PAUSED(watermark)" if p.stats.paused_for_backpressure else "flowing",
                p.stats.frames_in, p.stats.frames_out,
            )
        except Exception:
            # Stats are optional diagnostics. A partial pipeline (notably one
            # still opening or a test/fallback implementation) must never
            # prevent the teardown acknowledgement and resource cleanup.
            log.debug("session %s stats unavailable", session.id[:6], exc_info=True)

    async def _stats_loop(self) -> None:
        """One line per active session every stats_interval seconds: queue/cache
        depths through the pipeline, ONNX inference rate, and pacing state."""
        while True:
            await asyncio.sleep(self.stats_interval)
            for session in {s.id: s for s in self.sessions.values()}.values():
                p = session.pipeline
                if p is None or session.state.value == "closed":
                    continue
                self._log_session_stats(session)

    def _emit_event(self, message: str) -> None:
        if self.event_callback is not None:
            try:
                self.event_callback(message)
            except Exception:
                log.exception("event callback failed")

    def _retire_session(self, session: Session, closing: asyncio.Future) -> bool:
        """Take a session out of /status now that its native close has returned.

        GET /status is a client contract (docs/PROTOCOL.md): a failed close is
        recorded before the session disappears, both in this one synchronous
        step, so a poller never sees the session gone without
        ``restart_required`` set. Returns whether the close succeeded.
        """
        error = (
            asyncio.CancelledError("session close was cancelled")
            if closing.cancelled() else closing.exception()
        )
        if error is not None:
            self.native_teardown_error = {
                "session_id": session.id,
                "error": repr(error),
                "restart_required": True,
            }
            log.error(
                "session %s native teardown did not complete; server restart required",
                session.id, exc_info=error,
            )
        for key in (session.uplink_token, session.downlink_token, session.id):
            self.sessions.pop(key, None)
        return error is None

    async def _close_control_session(
        self, session: Session, ws: web.WebSocketResponse | None,
        acknowledge: bool,
    ) -> bool:
        """Release a session and optionally publish the teardown barrier."""
        # At once: the close below starts on the next loop turn, and a resume
        # must not take the session over in between.
        session.mark_closing()
        self._log_session_stats(session, final=True)
        closing = asyncio.ensure_future(session.close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            # This handler is being cancelled (server shutdown), but the native
            # close is shielded and may still be running. The session leaves
            # /status when that close returns, not now.
            if closing.done():
                self._retire_session(session, closing)
            else:
                closing.add_done_callback(functools.partial(self._retire_session, session))
            raise
        except Exception:
            pass  # read back from the finished close below
        cleanup_ok = self._retire_session(session, closing)
        if ws is None:
            return cleanup_ok
        if acknowledge and cleanup_ok and not ws.closed:
            try:
                await ws.send_str(json.dumps({"type": "closed"}))
            except (ConnectionResetError, RuntimeError):
                pass
        elif acknowledge and not cleanup_ok and not ws.closed:
            await ws.close(
                code=1011,
                message=b"native teardown incomplete; server restart required",
            )
        return cleanup_ok

    # -- control channel -------------------------------------------------------

    async def handle_control(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=CONTROL_HEARTBEAT_S)
        await ws.prepare(request)
        self._control_sockets.add(ws)
        peer_ip = request.remote or "unknown"
        if peer_ip not in self._seen_ips:
            self._seen_ips.add(peer_ip)
            log.info("control connection from %s (new client)", peer_ip)
            self._emit_event(f"New client connected: {peer_ip}")
        else:
            log.info("control connection from %s", peer_ip)
        session: Session | None = None
        teardown_ack_requested = False
        # The connection ended under the loop (lost, or closed by the peer),
        # not by a teardown, an error or this handler failing: only then is a
        # resumable session held.
        connection_ended = False
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(raw.data)
                    mtype = msg["type"]
                except (json.JSONDecodeError, KeyError):
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "bad_message", "message": "not json", "fatal": False}
                    ))
                    continue

                if mtype == "hello":
                    if int(msg.get("protocol_version", 0)) != PROTOCOL_VERSION:
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "unsupported_version",
                            "message": f"server speaks v{PROTOCOL_VERSION}", "fatal": True,
                        }))
                        break
                    await ws.send_str(json.dumps({
                        "type": "capabilities",
                        "protocol_version": PROTOCOL_VERSION,
                        "server_name": "upscale-relay",
                        "models": [
                            {"name": n, "scale_factor": i["scale_factor"]}
                            for n, i in self.models_info.items()
                        ] + [{"name": "passthrough", "scale_factor": 1}],
                        "quality_tiers": list(QUALITY_OPTION_IDS),
                        "quality_options": list(QUALITY_OPTIONS),
                        "resize_algorithms": list(RESIZE_ALGORITHMS),
                        "default_resize_algorithm": self.resize_algorithm,
                        "library": self.library is not None,
                        # Opt-in: old clients keep the video-only downlink and
                        # /media external-track path until they explicitly
                        # request muxed auxiliary tracks in open_session.
                        "muxed_aux_tracks": self.library is not None,
                        "attachment_cache": 1 if self.library is not None else 0,
                        # open_session.high_bit_depth is understood: HDR
                        # sources stay 10-bit, and on request every source.
                        "high_bit_depth": True,
                        # open_session/seek display_target is understood
                        # (PROTOCOL §3.6): a working mapper was found.
                        "display_mapping": self.display_mapper is not None,
                        # open_session.resume and resume_session are understood
                        # (PROTOCOL §1.2).
                        "session_resume": self.resume_grace_s > 0,
                        "library_sort": (
                            list(LIBRARY_SORT_KEYS) if self.library is not None else []
                        ),
                        **({"server_id": self.server_id} if self.server_id else {}),
                    }))
                elif mtype == "open_session":
                    if self.native_teardown_error is not None:
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "server_restart_required",
                            "message": (
                                "a previous native session teardown did not complete; "
                                "restart the relay server before opening another session"
                            ),
                            "fatal": True,
                        }))
                        break
                    if session is not None:
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "bad_message",
                            "message": "session already open", "fatal": False,
                        }))
                        continue
                    session = Session(
                        ws, self.models, ep=self.ep, library=self.library,
                        default_resize_algorithm=self.resize_algorithm,
                        lossless_hevc_profile=self.lossless_hevc_profile,
                        seek_discard_max_s=self.seek_discard_max_s,
                        display_mapper=self.display_mapper,
                        resume_grace_s=self.resume_grace_s,
                    )
                    session.media_port = self.media_port
                    self.sessions[session.uplink_token] = session
                    self.sessions[session.downlink_token] = session
                    self.sessions[session.id] = session
                    # Run the open in the background so this loop stays inside
                    # receive(): aiohttp only answers WS pings there, and a
                    # first-use TensorRT engine build blocks handle_open for
                    # minutes — awaiting it here left client pings unanswered
                    # (OkHttp drops the socket after 10 s without a pong).
                    session.begin_open(msg)
                elif mtype == "resume_session":
                    if session is not None:
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "bad_message",
                            "message": "session already open", "fatal": False,
                        }))
                        continue
                    resumed = self._take_over_session(msg, ws)
                    if resumed is None:
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "resume_failed",
                            "message": "no session to resume", "fatal": False,
                        }))
                        continue
                    session = resumed
                    log.info("session %s resumed from %s", session.id, peer_ip)
                    # After a seek the old connection had in flight: the
                    # snapshot must not report an epoch still being flushed.
                    async with session.seek_lock:
                        report = session.resume_report()
                    if session.ws is not ws:
                        break  # resumed again meanwhile: the newer one answers
                    if session.closing.is_set():
                        # The seek it waited for failed; the session is ending.
                        await ws.send_str(json.dumps({
                            "type": "error", "code": "resume_failed",
                            "message": "the session is closing", "fatal": False,
                        }))
                        break
                    await session.send("session_resumed", **report)
                elif session is None:
                    await ws.send_str(json.dumps({
                        "type": "error", "code": "bad_message",
                        "message": f"'{mtype}' before open_session", "fatal": False,
                    }))
                elif session.ws is not ws:
                    # A resume took the session over (PROTOCOL §1.2): what is
                    # still queued on this connection is no longer its to say.
                    break
                elif mtype == "play":
                    starting = session.state == State.OPEN
                    await session.set_state(State.PLAYING)
                    if starting:
                        source = session.source_path or "client uplink"
                        log.info("playback started: session %s source=%s client=%s",
                                 session.id, source, peer_ip)
                        self._emit_event(f"Playback started: {source} ({peer_ip})")
                elif mtype == "pause":
                    await session.set_state(State.PAUSED)
                elif mtype == "seek":
                    await session.handle_seek(msg)
                elif mtype == "buffer_report":
                    session.handle_buffer_report(msg)
                elif mtype == "teardown":
                    teardown_ack_requested = True
                    break
                else:
                    await session.send("error", code="bad_message", message=mtype, fatal=False)
            else:
                connection_ended = True
        finally:
            if session is not None and session.ws is not ws and not ws.closed:
                # Taken over, and leaving before the background close: say
                # so, or the displaced client reads a normal close and tries
                # to take the session back.
                try:
                    await asyncio.wait_for(ws.close(
                        code=CLOSE_SESSION_TAKEN_OVER,
                        message=b"session resumed on another connection"), 2.0)
                except Exception:
                    pass
            # A session that a resume took over belongs to its new connection.
            if session is not None and session.ws is ws:
                # Held only when the connection was lost: a client that closed
                # it normally is there, and chose to end.
                if (connection_ended and session.resumable
                        and ws.close_code != WSCloseCode.OK and self._stop_task is None):
                    self._hold_session(session)
                else:
                    await self._close_control_session(
                        session, ws, teardown_ack_requested,
                    )
        return ws

    # -- resume (PROTOCOL §1.2) ---------------------------------------------------

    def _hold_session(self, session: Session) -> None:
        session.detach()
        self._held[session.id] = asyncio.create_task(self._expire_held(session))

    async def _expire_held(self, session: Session) -> None:
        """Close a held session when its grace period ends without a resume,
        or as soon as it closed itself (a pipeline error) meanwhile."""
        try:
            await asyncio.wait_for(session.closing.wait(), session.resume_grace_s)
        except asyncio.TimeoutError:
            pass
        if session.ws is not None:
            return  # resumed (the resume cancels this task too)
        self._held.pop(session.id, None)
        if not session.closing.is_set():
            log.info("session %s: no resume within %.0f s; closing it",
                     session.id, session.resume_grace_s)
        await self._close_control_session(session, None, False)

    def _take_over_session(self, msg: dict, ws: web.WebSocketResponse) -> Session | None:
        """The session ``resume_session`` names, now on ``ws``; None if refused.

        The session may still be on a connection this server has not yet
        noticed is dead (the client gave up on it first): that connection is
        closed, and its handler leaves the session alone.
        """
        session_id, token = msg.get("session_id"), msg.get("resume_token")
        if (not isinstance(session_id, str) or not isinstance(token, str)
                or not token.isascii()):  # tokens are hex; compare_digest needs ASCII
            return None
        session = self.sessions.get(session_id)
        if (session is None or session.id != session_id or session.resume_token is None
                or not hmac.compare_digest(session.resume_token, token)
                or not session.resumable or self._stop_task is not None):
            return None
        held = self._held.pop(session.id, None)
        if held is not None:
            held.cancel()
        previous = session.ws
        session.attach(ws)
        if previous is not None and previous is not ws:
            task = asyncio.create_task(self._close_superseded(previous))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return session

    @staticmethod
    async def _close_superseded(ws: web.WebSocketResponse) -> None:
        try:
            await asyncio.wait_for(
                ws.close(code=CLOSE_SESSION_TAKEN_OVER,
                         message=b"session resumed on another connection"),
                2.0,
            )
        except Exception:  # most likely the dead connection the client left
            pass

    # -- media sockets -----------------------------------------------------------

    async def handle_media(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._media_handlers[task] = writer
        peer = writer.get_extra_info("peername")
        attached_session = None
        direction = None
        try:
            direction, token = parse_handshake(await reader.readexactly(HANDSHAKE_LEN))
            session = self.sessions.get(token)
            valid = session is not None and (
                (direction == DIR_UPLINK and token == session.uplink_token
                 and session.source_kind == "uplink")
                or (direction == DIR_DOWNLINK and token == session.downlink_token)
            )
            if not valid or not session.register_media(direction, writer):
                writer.write(b"\x01")
                await writer.drain()
                return
            attached_session = session
            writer.write(b"\x00")
            await writer.drain()
            if direction == DIR_UPLINK:
                await self._run_uplink(session, reader)
            else:
                await self._run_downlink(session, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError) as err:
            log.info("media conn %s closed: %r", peer, err)
        finally:
            if attached_session is not None:
                attached_session.unregister_media(direction, writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass
            finally:
                self._media_handlers.pop(task, None)

    async def _run_uplink(self, session: Session, reader: asyncio.StreamReader) -> None:
        while session.state != State.CLOSED:
            pkt = await read_packet(reader)
            if pkt.epoch < session.epoch:
                continue  # stale, drop before any processing
            if session.pipeline is None:
                continue
            # Blocking put in executor -> natural TCP backpressure.
            await asyncio.to_thread(session.pipeline.feed, pkt)

    async def _run_downlink(self, session: Session, writer: asyncio.StreamWriter) -> None:
        await session.start_server_source()
        while True:
            pkt = await session.down_q.get()
            if pkt is None or session.state == State.CLOSED:
                break
            if pkt.epoch < session.epoch:
                continue
            writer.write(encode_packet(pkt))
            await writer.drain()

    # -- status -------------------------------------------------------------------

    async def handle_status(self, request: web.Request) -> web.Response:
        unique = {s.id: s for s in self.sessions.values()}
        return web.json_response({
            "server": "upscale-relay",
            "protocol_version": PROTOCOL_VERSION,
            "default_resize_algorithm": self.resize_algorithm,
            "lossless_hevc_profile": self.lossless_hevc_profile,
            "restart_required": self.native_teardown_error is not None,
            "native_teardown_error": self.native_teardown_error,
            "models": list(self.models_info) + ["passthrough"],
            "sessions": [s.status() for s in unique.values()],
        })

    async def handle_library(self, request: web.Request) -> web.Response:
        assert self.library is not None
        relative = request.query.get("path", "")
        sort = request.query.get("sort", "name")
        try:
            offset = int(request.query.get("cursor", "0"))
            limit = min(int(request.query.get("limit", "100")), 500)
            tree, next_cursor = await asyncio.to_thread(
                self.library.page, relative, offset=offset, limit=limit, sort=sort,
            )
        except LibraryPathError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response({"tree": tree, "next_cursor": next_cursor})

    async def handle_media_file(self, request: web.Request) -> web.StreamResponse:
        assert self.library is not None
        try:
            path = await asyncio.to_thread(
                self.library.resolve_file, request.match_info["path"],
            )
        except LibraryPathError:
            raise web.HTTPNotFound()
        # aiohttp FileResponse implements byte ranges, conditional requests,
        # and streaming without reading the whole media file into memory.
        return web.FileResponse(path)

    async def handle_attachment(self, request: web.Request) -> web.Response:
        """Serve one immutable object authorized by an active session manifest."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        token = auth.removeprefix("Bearer ").strip()
        digest = request.match_info["digest"].lower()
        if not _SHA256_RE.fullmatch(digest):
            raise web.HTTPNotFound()
        session = next(
            (
                item for item in {value.id: value for value in self.sessions.values()}.values()
                if item.attachment_token == token and item.state != State.CLOSED
            ),
            None,
        )
        if session is None or session.aux_track is None:
            raise web.HTTPUnauthorized()
        attachment = session.aux_track.attachment_by_hash(digest)
        if attachment is None or not any(
            entry.get("sha256") == digest for entry in session.attachment_manifest
        ):
            raise web.HTTPNotFound()
        return web.Response(
            body=attachment.data,
            content_type=attachment.mimetype,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if self._media_server is not None or self._runner is not None:
            raise RuntimeError("relay server already started")
        self._stop_task = None
        try:
            self._media_server = await asyncio.start_server(self.handle_media, port=self.media_port)
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, port=self.port)
            await site.start()
            if self.stats_interval:
                self._stats_task = asyncio.create_task(self._stats_loop())
            if self._mdns is not None:
                await self._mdns.start()
            log.info("control/status on :%d, media on :%d", self.port, self.media_port)
        except BaseException:
            # A failed control bind or canceled advertisement must not leave
            # the earlier media listener alive in an unpublished server.
            try:
                await self.stop()
            except BaseException:
                log.exception("server startup rollback failed")
            raise

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_impl())
        await asyncio.shield(self._stop_task)

    async def _stop_impl(self) -> None:
        errors = []
        media, self._media_server = self._media_server, None
        runner, self._runner = self._runner, None
        if media is not None:
            media.close()  # stop accepting; await only after active sockets close
        if self._stats_task is not None:
            self._stats_task.cancel()
            await asyncio.gather(self._stats_task, return_exceptions=True)
            self._stats_task = None
        if self._mdns is not None:
            try:
                await self._mdns.stop()
            except Exception as error:
                errors.append(error)
        held, self._held = list(self._held.values()), {}
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)
        for session in {s.id: s for s in self.sessions.values()}.values():
            if session.ws is None:
                # Held for a resume: no handler owns it to retire it.
                if not await self._close_control_session(session, None, False):
                    errors.append(RuntimeError(
                        f"held session {session.id} did not close: "
                        f"{(self.native_teardown_error or {}).get('error')}"))
                continue
            try:
                await session.close()
            except Exception as error:
                errors.append(error)
            finally:
                try:
                    await session.ws.close()
                except Exception as error:
                    errors.append(error)
        # Include peers still waiting for a handshake, which have no Session
        # owner yet. Python 3.13+ wait_closed also waits for these transports.
        handlers = list(self._media_handlers.items())
        for task, writer in handlers:
            writer.close()
            writer.transport.abort()
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *(task for task, _writer in handlers if task is not asyncio.current_task()),
            return_exceptions=True,
        )
        if media is not None:
            await media.wait_closed()
        for ws in list(self._control_sockets):
            try:
                await asyncio.wait_for(
                    ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown"), 2.0,
                )
            except Exception as error:  # an unresponsive peer must not stall shutdown
                log.warning("control socket did not close cleanly: %r", error)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as error:
                errors.append(error)
        if not errors and self.native_teardown_error is not None:
            # A native owner may still be alive, whenever its close failed
            # (a held session can expire during this stop): a new server in
            # this process would start with restart_required clear.
            errors.append(RuntimeError(
                "a native session teardown did not complete; restart the process: "
                f"{self.native_teardown_error.get('error')}"))
        if errors:
            raise errors[0]


async def main_async(args) -> None:
    from .server_id import load_or_create_server_id

    stopping = asyncio.Event()
    if sys.platform != "win32":
        # systemctl stop sends SIGTERM, whose default action ends the process
        # at once: no mDNS goodbye, no session teardown, temporary subtitle
        # indexes left behind. Installed first, so a stop during start-up
        # (mDNS registration takes a second) is handled too.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)

    server_id = await asyncio.to_thread(load_or_create_server_id)
    display_mapper = (
        None if args.no_display_mapping
        else await asyncio.to_thread(DisplayMapper.detect, args.ffmpeg)
    )
    server = RelayServer(
        args.models_dir, args.port, ep=args.ep,
        stats_interval=2.0 if args.verbose else None,
        library_roots=args.library,
        resize_algorithm=args.resize_algorithm,
        lossless_hevc_profile=args.lossless_hevc_profile,
        mdns=not args.no_mdns,
        seek_discard_max_s=args.seek_discard_max_s,
        server_id=server_id,
        display_mapper=display_mapper,
    )
    try:
        await server.start()
        await stopping.wait()
    finally:
        # Also on Ctrl-C, which asyncio.run delivers as a cancellation.
        await server.stop()
        log.info("server stopped")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-server")
    parser.add_argument("--models-dir", default="models")
    parser.add_argument("--port", type=int, default=8590)
    parser.add_argument("--ep", default="auto", choices=["auto", "tensorrt", "cuda", "dml", "cpu"])
    parser.add_argument(
        "--library", action="append", metavar="FOLDER",
        help="folder (local, UNC, or mounted share) to expose; repeat for multiple folders",
    )
    parser.add_argument(
        "--resize-algorithm", choices=RESIZE_ALGORITHMS,
        default=DEFAULT_RESIZE_ALGORITHM,
        help="default filter for the post-ONNX resize (clients may override it)",
    )
    parser.add_argument(
        "--lossless-hevc-profile", choices=LOSSLESS_HEVC_PROFILES,
        default=DEFAULT_LOSSLESS_HEVC_PROFILE,
        help="server-wide lossless HEVC encoder experiment",
    )
    parser.add_argument(
        "--seek-discard-max-s", type=float, default=None, metavar="SECONDS",
        help="trade seek accuracy for latency: when a seek lands on a keyframe "
             "more than SECONDS before the target, start the new epoch at the "
             "keyframe instead of decoding and dropping the frames in between "
             "(default: off, seeks stay frame-accurate)",
    )
    parser.add_argument(
        "--ffmpeg", metavar="PATH",
        help="ffmpeg with libplacebo (Vulkan) that applies Dolby Vision and HDR10+ "
             "for a client's display (default: $RELAY_FFMPEG, else ffmpeg on PATH)",
    )
    parser.add_argument(
        "--no-display-mapping", action="store_true",
        help="do not offer display mapping (PROTOCOL 3.6), even with an ffmpeg that can",
    )
    parser.add_argument(
        "--no-mdns", action="store_true",
        help="do not advertise _upscalerelay._tcp over mDNS/DNS-SD",
    )
    if sys.platform.startswith("linux"):
        # Windows source installs use the .[nvidia] extra, and the Windows
        # release binaries set up their runtime themselves at first launch.
        parser.add_argument(
            "--install-nvidia-runtime", action="store_true",
            help="download and verify the pinned ONNX Runtime and TensorRT for model "
                 "sessions (about 4 GB, into ~/.local/share/upscale-relay/runtimes; "
                 "CUDA and cuDNN come from the system), then exit. Restart a "
                 "running server afterwards",
        )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _activate_linux_runtime() -> None:
    """Linux: put the managed NVIDIA runtime in place before anything imports
    onnxruntime, or say how to get one.  Passthrough sessions never need it."""
    import importlib.util

    from .runtime_bootstrap import activate_runtime, runtime_dir

    if activate_runtime():
        log.info("NVIDIA runtime: %s", runtime_dir())
    elif importlib.util.find_spec("onnxruntime") is None:
        log.warning(
            "ONNX Runtime is not installed, so only passthrough sessions can run. "
            "Install the TensorRT runtime with: relay-server --install-nvidia-runtime, "
            "then restart the server"
        )


def main() -> None:
    # Native faults (libav/ORT/TRT) kill the process silently otherwise —
    # print the Python-level stack of the faulting thread instead.
    import faulthandler

    faulthandler.enable()
    try:
        from .crashinfo import install as _install_crashinfo

        _install_crashinfo()
    except Exception:
        pass

    args = build_arg_parser().parse_args()
    if getattr(args, "install_nvidia_runtime", False):
        from .runtime_bootstrap import install_runtime

        sys.exit(install_runtime())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if sys.platform.startswith("linux"):
        _activate_linux_runtime()
    from .instance_lock import AlreadyRunningError, acquire_instance_lock

    try:
        instance_lock = acquire_instance_lock()
    except AlreadyRunningError as error:
        print(f"relay-server: {error}", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
