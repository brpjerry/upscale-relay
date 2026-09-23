import asyncio
import threading

from relay_client_core.client import _ThreadBridgeQueue
from relay_protocol import MediaPacket


def test_large_payloads_publish_before_packet_batch_is_full(monkeypatch):
    from relay_client_core import client

    monkeypatch.setattr(client, "_DOWNLINK_BATCH_BYTES", 8)
    batch = []
    assert client._take_downlink_batch(batch, MediaPacket(b"12345"), 0) is None
    ready = client._take_downlink_batch(batch, MediaPacket(b"67890"), 0)
    assert [packet.payload for packet in ready] == [b"12345", b"67890"]
    assert batch == []


def test_batch_larger_than_budget_is_delivered_without_loss_or_deadlock():
    async def scenario():
        queue = _ThreadBridgeQueue(asyncio.get_running_loop(), maxsize=4, max_bytes=8)
        packets = [MediaPacket(bytes([i]) * 5) for i in range(10)]
        worker = threading.Thread(target=queue.put_batch_from_thread, args=(packets + [None],))
        worker.start()
        received = []
        try:
            while True:
                packet = await asyncio.wait_for(queue.get(), 2)
                if packet is None:
                    break
                received.append(packet)
                with queue._condition:
                    assert queue._queued_bytes <= 8
            assert received == packets
        finally:
            queue.close()
            await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        assert queue._queued_bytes == 0

    asyncio.run(scenario())


def test_close_releases_producer_waiting_for_byte_capacity():
    async def scenario():
        queue = _ThreadBridgeQueue(asyncio.get_running_loop(), maxsize=10, max_bytes=8)
        queue.put_batch_from_thread([MediaPacket(b"12345678")])
        worker = asyncio.create_task(asyncio.to_thread(
            queue.put_batch_from_thread, [MediaPacket(b"x")]))
        await asyncio.sleep(0)
        queue.close()
        assert await asyncio.wait_for(worker, 2) is False
        assert await queue.get() is None

    asyncio.run(scenario())
