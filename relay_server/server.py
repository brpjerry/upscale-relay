"""Relay server entry point: aiohttp control/status + TCP media listener.

    python -m relay_server.server --models-dir models --port 8590

Control WebSocket: ws://host:8590/control
Status:            http://host:8590/status
Media (TCP):       host:8591 (control port + 1)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from relay_protocol import (
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

from .mdns import MdnsAdvertiser
from .session import Session, State
from .library import LibraryPathError, MediaLibrary

log = logging.getLogger("relay.server")


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


class RelayServer:
    def __init__(self, models_dir: str, port: int, ep: str = "auto",
                 stats_interval: float | None = None,
                 library_root: str | None = None,
                 resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM,
                 lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE,
                 mdns: bool = False):
        self.port = port
        self.media_port = port + 1
        self.ep = ep
        self.stats_interval = stats_interval  # seconds; None = no periodic stats
        self.resize_algorithm = resize_algorithm
        self.lossless_hevc_profile = lossless_hevc_profile
        self.models_info = discover_models(models_dir)
        self.models = {name: info["path"] for name, info in self.models_info.items()}
        self.library = MediaLibrary(library_root) if library_root else None
        self.sessions: dict[str, Session] = {}  # keyed by media tokens AND id
        self.app = web.Application()
        self.app.router.add_get("/control", self.handle_control)
        self.app.router.add_get("/status", self.handle_status)
        if self.library is not None:
            self.app.router.add_get("/library", self.handle_library)
            self.app.router.add_get("/media/{path:.*}", self.handle_media_file)
        self._stats_task = None
        self._mdns = MdnsAdvertiser(
            port=self.port,
            media_port=self.media_port,
            server_name="upscale-relay",
            protocol_version=PROTOCOL_VERSION,
        ) if mdns else None

    # -- periodic stats (--verbose) ----------------------------------------------

    @staticmethod
    def _log_session_stats(session: Session, *, final: bool = False) -> None:
        """Persist the live /status performance fields in a readable line."""
        p = session.pipeline
        if p is None:
            return
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
            "client buffer %5d ms (est %5d) | %s | frames %d in / %d out",
            session.id[:6], session.state.value, " FINAL" if final else "",
            session.epoch, session.source_kind,
            p.stats.fps, infer_fps, stage_str,
            p.out_w, p.out_h, p.downlink_codec, p.encoder_name, p.quality_tier,
            cached, depths["decoded"], depths["upscaled"],
            session.down_q.qsize(), depths["in"],
            p.client_buffered_ms, p.buffered_ms_now(),
            "PAUSED(watermark)" if p.stats.paused_for_backpressure else "flowing",
            p.stats.frames_in, p.stats.frames_out,
        )

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

    # -- control channel -------------------------------------------------------

    async def handle_control(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        session: Session | None = None
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
                    }))
                elif mtype == "open_session":
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
                elif session is None:
                    await ws.send_str(json.dumps({
                        "type": "error", "code": "bad_message",
                        "message": f"'{mtype}' before open_session", "fatal": False,
                    }))
                elif mtype == "play":
                    await session.set_state(State.PLAYING)
                elif mtype == "pause":
                    await session.set_state(State.PAUSED)
                elif mtype == "seek":
                    await session.handle_seek(msg)
                elif mtype == "buffer_report":
                    session.handle_buffer_report(msg)
                elif mtype == "teardown":
                    await session.send("closed")
                    break
                else:
                    await session.send("error", code="bad_message", message=mtype, fatal=False)
        finally:
            if session is not None:
                self._log_session_stats(session, final=True)
                await session.close()
                for key in (session.uplink_token, session.downlink_token, session.id):
                    self.sessions.pop(key, None)
        return ws

    # -- media sockets -----------------------------------------------------------

    async def handle_media(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            direction, token = parse_handshake(await reader.readexactly(HANDSHAKE_LEN))
            session = self.sessions.get(token)
            valid = session is not None and (
                (direction == DIR_UPLINK and token == session.uplink_token
                 and session.source_kind == "uplink")
                or (direction == DIR_DOWNLINK and token == session.downlink_token)
            )
            if not valid:
                writer.write(b"\x01")
                await writer.drain()
                return
            writer.write(b"\x00")
            await writer.drain()
            if direction == DIR_UPLINK:
                await self._run_uplink(session, reader)
            else:
                await self._run_downlink(session, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError) as err:
            log.info("media conn %s closed: %r", peer, err)
        finally:
            writer.close()

    async def _run_uplink(self, session: Session, reader: asyncio.StreamReader) -> None:
        session.uplink_attached = True
        try:
            while session.state != State.CLOSED:
                pkt = await read_packet(reader)
                if pkt.epoch < session.epoch:
                    continue  # stale, drop before any processing
                if session.pipeline is None:
                    continue
                # Blocking put in executor -> natural TCP backpressure.
                await asyncio.to_thread(session.pipeline.feed, pkt)
        finally:
            session.uplink_attached = False

    async def _run_downlink(self, session: Session, writer: asyncio.StreamWriter) -> None:
        session.downlink_attached = True
        try:
            await session.start_server_source()
            while True:
                pkt = await session.down_q.get()
                if pkt is None or session.state == State.CLOSED:
                    break
                if pkt.epoch < session.epoch:
                    continue
                writer.write(encode_packet(pkt))
                await writer.drain()
        finally:
            session.downlink_attached = False

    # -- status -------------------------------------------------------------------

    async def handle_status(self, request: web.Request) -> web.Response:
        unique = {s.id: s for s in self.sessions.values()}
        return web.json_response({
            "server": "upscale-relay",
            "protocol_version": PROTOCOL_VERSION,
            "default_resize_algorithm": self.resize_algorithm,
            "lossless_hevc_profile": self.lossless_hevc_profile,
            "models": list(self.models_info) + ["passthrough"],
            "sessions": [s.status() for s in unique.values()],
        })

    async def handle_library(self, request: web.Request) -> web.Response:
        assert self.library is not None
        relative = request.query.get("path", "")
        try:
            offset = int(request.query.get("cursor", "0"))
            limit = min(int(request.query.get("limit", "100")), 500)
            tree, next_cursor = await asyncio.to_thread(
                self.library.page, relative, offset=offset, limit=limit,
            )
        except LibraryPathError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response({"tree": tree, "next_cursor": next_cursor})

    async def handle_media_file(self, request: web.Request) -> web.StreamResponse:
        assert self.library is not None
        try:
            path = self.library.resolve_file(request.match_info["path"])
        except LibraryPathError:
            raise web.HTTPNotFound()
        # aiohttp FileResponse implements byte ranges, conditional requests,
        # and streaming without reading the whole media file into memory.
        return web.FileResponse(path)

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
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

    async def stop(self) -> None:
        if self._mdns is not None:
            await self._mdns.stop()
        if self._stats_task is not None:
            self._stats_task.cancel()
        for s in {s.id: s for s in self.sessions.values()}.values():
            await s.close()
        self._media_server.close()
        await self._media_server.wait_closed()
        await self._runner.cleanup()


async def main_async(args) -> None:
    server = RelayServer(
        args.models_dir, args.port, ep=args.ep,
        stats_interval=2.0 if args.verbose else None,
        library_root=args.library,
        resize_algorithm=args.resize_algorithm,
        lossless_hevc_profile=args.lossless_hevc_profile,
        mdns=not args.no_mdns,
    )
    await server.start()
    await asyncio.Event().wait()  # run forever


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

    parser = argparse.ArgumentParser(prog="relay-server")
    parser.add_argument("--models-dir", default="models")
    parser.add_argument("--port", type=int, default=8590)
    parser.add_argument("--ep", default="auto", choices=["auto", "tensorrt", "cuda", "dml", "cpu"])
    parser.add_argument("--library", help="folder (local, UNC, or mounted share) to expose")
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
        "--no-mdns", action="store_true",
        help="do not advertise _upscalerelay._tcp over mDNS/DNS-SD",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
