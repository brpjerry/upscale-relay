"""Shared adjacent server ports, below Linux/Windows default ephemeral ranges."""

import random
import socket


# Never reuse a pair in this test process, including across test modules. The
# old 40000–55000 range overlapped Linux's outgoing TCP port allocator, which
# could claim a just-probed port before RelayServer bound its listeners.
_candidates = iter(random.sample(range(20000, 30000, 2), 5000))


def free_port_pair() -> int:
    for port in _candidates:
        try:
            with socket.socket() as control, socket.socket() as media:
                # Match RelayServer's wildcard bind, not just loopback.
                control.bind(("0.0.0.0", port))
                media.bind(("0.0.0.0", port + 1))
            return port
        except OSError:
            continue
    raise RuntimeError("no free test server port pair below 30000")
