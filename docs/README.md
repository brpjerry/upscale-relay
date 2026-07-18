# Documentation

Project documentation lives here; repository entry points and coding-agent
instructions remain at the root.

## Architecture and protocol

- [Architecture and roadmap](PLAN.md) — design decisions, shipped components,
  remaining work, and future phases.
- [Wire protocol](PROTOCOL.md) — control messages, media framing, PTS/epoch
  semantics, seeks, and backpressure.
- [Server-side media library](SERVER_LIBRARY.md) — the implemented `--library`
  feature, HTTP delivery, client UI, and the remaining shared-mount mapping
  work.
- [Android client plan](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_CLIENT.md) — selected architecture, robust
  server-library MVP, feature-parity phases, and device acceptance gates.
- [Android device validation](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_DEVICE_NOTES.md) — Phase 1 robustness and
  Phase 2 A/V, controls, seek, tier, and subtitle evidence.

## Setup and operations

- [Linux client setup](CLIENT_LINUX.md) — dependencies, installation, flags,
  firewall setup, and bandwidth guidance.

## Measurements

- [Quality tier notes](TIER_NOTES.md) — codec behavior, measured bitrates,
  decoder notes, and client transport findings.
- [Benchmark results](BENCH.md) — model, decode, encode, and TensorRT results.

Roadmap statuses use these meanings:

- **Implemented** — present in the current code and covered by tests or
  operational use.
- **Partial** — a usable subset exists, with explicitly listed work remaining.
- **Planned** — not implemented in this repository yet.
