import socket

import ports


def test_pairs_are_distinct_and_below_default_ephemeral_ranges():
    pairs = [ports.free_port_pair() for _ in range(20)]
    assert len(set(pairs)) == len(pairs)
    assert all(20000 <= port < 30000 and port % 2 == 0 for port in pairs)


def test_busy_media_port_rejects_the_whole_pair(monkeypatch):
    busy, available = ports.free_port_pair(), ports.free_port_pair()
    monkeypatch.setattr(ports, "_candidates", iter([busy, available]))
    with socket.socket() as media:
        media.bind(("0.0.0.0", busy + 1))
        assert ports.free_port_pair() == available
