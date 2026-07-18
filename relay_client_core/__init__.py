"""Reusable client-side building blocks: demux/uplink, control channel,
downlink receive. The mock CLI (phase 2) and the desktop GUI (phase 3) are
thin shells over this package."""

from .client import RelayClient, SessionConfig  # noqa: F401
from .demux import VideoTrack  # noqa: F401
