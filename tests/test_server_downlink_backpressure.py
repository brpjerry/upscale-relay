import asyncio
import threading
import time

from relay_protocol import MediaPacket
from relay_server.session import Session, _DownlinkQueue


class _Ws:
    async def send_str(self, _message):
        pass


def test_worker_burst_is_bounded_without_losing_container_bytes():
    async def scenario():
        session = Session(_Ws(), {})
        completed = []

        def produce():
            for index in range(600):
                session._emit_downlink(MediaPacket(payload=index.to_bytes(2)))
                completed.append(index)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        # Deliberately occupy the event loop while the native worker produces.
        time.sleep(0.05)
        assert not completed
        await asyncio.sleep(0.15)
        assert session.down_q.qsize() == session.down_q.maxsize
        assert len(completed) <= session.down_q.maxsize
        received = [
            int.from_bytes((await asyncio.wait_for(session.down_q.get(), 2)).payload)
            for _ in range(600)
        ]
        await asyncio.to_thread(producer.join, 2)
        assert not producer.is_alive()
        assert received == list(range(600))
        await session.close()

    asyncio.run(scenario())


def test_close_releases_worker_waiting_for_downlink_capacity():
    async def scenario():
        session = Session(_Ws(), {})
        for _ in range(session.down_q.maxsize):
            session.down_q.put_nowait(MediaPacket(payload=b"queued"))
        producer = threading.Thread(
            target=session._emit_downlink, args=(MediaPacket(payload=b"pending"),), daemon=True,
        )
        producer.start()
        await asyncio.sleep(0.02)
        await session.close()
        await asyncio.to_thread(producer.join, 1)
        assert not producer.is_alive()
        await asyncio.sleep(0)
        assert session.down_q.get_nowait() is None
        assert session.down_q.empty()

    asyncio.run(scenario())


def test_downlink_queue_applies_byte_backpressure_and_releases_on_get():
    async def scenario():
        queue = _DownlinkQueue(maxsize=256, max_bytes=16)
        first = MediaPacket(payload=b"a" * 10)
        second = MediaPacket(payload=b"b" * 10)
        await queue.put(first)
        pending = asyncio.create_task(queue.put(second))
        await asyncio.sleep(0)
        assert not pending.done()
        assert queue.payload_bytes == 10
        assert queue.get_nowait() is first
        await asyncio.wait_for(pending, 1)
        assert queue.payload_bytes == 10
        assert queue.get_nowait() is second
        assert queue.payload_bytes == 0

    asyncio.run(scenario())
