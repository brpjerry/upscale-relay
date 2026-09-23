import asyncio
import json
from types import SimpleNamespace

from relay_server.pipeline import SeekTrace
from relay_server.session import Session
import relay_server.session as session_mod


def test_index_progress_extends_narration_but_stalled_index_still_expires(monkeypatch):
    async def scenario():
        now = [1.0]
        auxiliary = SimpleNamespace(subtitle_index_progress=(600.0, 10.0))
        reports = []

        class Ws:
            async def send_str(self, message):
                message = json.loads(message)
                if message["type"] != "seek_progress":
                    return
                reports.append(message)
                now[0] += 70.0
                if len(reports) < 3:
                    auxiliary.subtitle_index_progress = (600.0, 10.0 * (len(reports) + 1))

        monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INITIAL_DELAY_S", 0)
        monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INTERVAL_S", 0)
        monkeypatch.setattr(session_mod.time, "perf_counter", lambda: now[0])
        session = Session(Ws(), {})
        session.aux_track = auxiliary
        trace = SeekTrace(epoch=0, target_pts=600000, requested_at=0)
        await asyncio.wait_for(session._seek_progress_loop(trace, 0), 1)
        assert [report["subtitle_indexed_s"] for report in reports] == [10, 20, 30]
        assert reports[-1]["elapsed_s"] == 141.0
        assert all(report["stage"] == "subtitle_index" for report in reports)
        session.aux_track = None
        await session.close()

    asyncio.run(scenario())


def test_video_seek_without_index_progress_retains_stalled_cap(monkeypatch):
    async def scenario():
        reports = []
        class Ws:
            async def send_str(self, message):
                reports.append(json.loads(message))

        monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INITIAL_DELAY_S", 0)
        monkeypatch.setattr(session_mod.time, "perf_counter", lambda: 61.0)
        session = Session(Ws(), {})
        await session._seek_progress_loop(SeekTrace(0, 7000, 0), 0)
        assert not reports
        await session.close()

    asyncio.run(scenario())
