"""Quality-tier encoder selection.

Tiers map to candidate (codec, pix_fmt, options) lists in preference order;
the first encoder that actually opens on this machine wins (e.g. hevc_nvenc
needs both an ffmpeg build with NVENC and an NVIDIA GPU present).
"""

from __future__ import annotations

import os
import sys
import threading
from fractions import Fraction

import av

EncoderCandidate = tuple[str, str, dict[str, str]]

# The env override lets machines without an NVIDIA GPU (CI, CPU-only
# hosts) select a software profile such as "x265-ultrafast".
DEFAULT_LOSSLESS_HEVC_PROFILE = os.environ.get(
    "RELAY_LOSSLESS_HEVC_PROFILE", "nvenc-p4-low-delay"
)

# Public session choices. QP remains an implementation detail: clients render
# ``label`` and send the stable ``id``. The bandwidth figures are deliberately
# coarse P95 estimates for 4K-ish animated content, rounded to 50 Mbps. They
# describe a useful network/decode class, not a bitrate guarantee for const-QP.
QUALITY_OPTIONS: tuple[dict[str, object], ...] = (
    {
        "id": "lossless-hevc", "label": "True Lossless HEVC",
        "codec": "hevc", "lossless": True, "android_supported": True,
        "p95_mbps": None,
    },
    {
        "id": "hevc-qp2", "label": "HEVC ~350 Mbps",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 350,
    },
    {
        "id": "hevc-qp4", "label": "HEVC ~250 Mbps",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 250,
    },
    {
        "id": "hevc-qp6", "label": "HEVC ~200 Mbps",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 200,
    },
    {
        "id": "hevc-qp10", "label": "HEVC ~100 Mbps",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 100,
    },
    {
        "id": "hevc-qp14", "label": "HEVC ~50 Mbps (higher quality)",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 50,
    },
    {
        "id": "hevc-qp18", "label": "HEVC ~50 Mbps (lower bandwidth)",
        "codec": "hevc", "lossless": False, "android_supported": True,
        "p95_mbps": 50,
    },
    {
        "id": "lossless-ffv1", "label": "Lossless FFV1",
        "codec": "ffv1", "lossless": True, "android_supported": False,
        "p95_mbps": None,
    },
)

QUALITY_OPTION_IDS = tuple(str(option["id"]) for option in QUALITY_OPTIONS)

# Server-selectable experiments for hardware-decoder compatibility. Names
# describe encoder structure rather than promising an exact bitrate: lossless
# bitrate remains content-dependent. The expected rough order is documented in
# docs/TIER_NOTES.md and must be measured on the problem content.
LOSSLESS_HEVC_PROFILES: dict[str, list[EncoderCandidate]] = {
    # Backward-compatible control: historical NVENC settings, with the
    # x265 fallback when NVENC is unavailable. Pipeline supplies g=48.
    "auto": [
        ("hevc_nvenc", "yuv420p", {"tune": "lossless", "preset": "p7"}),
        ("libx265", "yuv420p", {"x265-params": "lossless=1", "preset": "medium"}),
    ],
    "nvenc-p7": [
        ("hevc_nvenc", "yuv420p", {"tune": "lossless", "preset": "p7"}),
    ],
    # Same P7/48-frame-GOP control with multiple independent slices per frame.
    # Some hardware decoders can process these in parallel; others gain
    # nothing, so keep them as explicit device experiments.
    "nvenc-p7-slices4": [
        ("hevc_nvenc", "yuv420p", {
            "tune": "lossless", "preset": "p7", "slices": "4",
        }),
    ],
    "nvenc-p7-slices8": [
        ("hevc_nvenc", "yuv420p", {
            "tune": "lossless", "preset": "p7", "slices": "8",
        }),
    ],
    # Near-lossless NVENC ladder. These deliberately do not use lossless
    # transform-bypass tuning; QP > 0 additionally introduces quantization.
    # All preserve the same 8-bit 4:2:0 HEVC/MediaCodec path.
    "nvenc-p7-qp0": [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": "0", "preset": "p7", "g": "240",
        }),
    ],
    "nvenc-p7-qp2": [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": "2", "preset": "p7", "g": "240",
        }),
    ],
    "nvenc-p7-qp4": [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": "4", "preset": "p7", "g": "240",
        }),
    ],
    "nvenc-p7-qp6": [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": "6", "preset": "p7", "g": "240",
        }),
    ],
    "nvenc-p7-qp8": [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": "8", "preset": "p7", "g": "240",
        }),
    ],
    # Isolates periodic lossless IDR spikes from other codec decisions.
    "nvenc-p7-long-gop": [
        ("hevc_nvenc", "yuv420p", {
            "tune": "lossless", "preset": "p7", "g": "240",
        }),
    ],
    # Decoder-light profiles: no B-frame reorder, one reference frame, no
    # lookahead/weighted prediction, and a ten-second GOP at ~24 fps.
    "nvenc-p4-low-delay": [
        ("hevc_nvenc", "yuv420p", {
            "tune": "lossless", "preset": "p4", "bf": "0", "refs": "1",
            "b_ref_mode": "disabled", "weighted_pred": "0",
            "rc-lookahead": "0", "zerolatency": "1", "g": "240",
        }),
    ],
    "nvenc-p1-low-delay": [
        ("hevc_nvenc", "yuv420p", {
            "tune": "lossless", "preset": "p1", "bf": "0", "refs": "1",
            "b_ref_mode": "disabled", "weighted_pred": "0",
            "rc-lookahead": "0", "zerolatency": "1", "g": "240",
        }),
    ],
    # CPU experiments. Ultrafast mirrors the simple low-delay dependency
    # structure; medium favors compression and is expected to be much slower.
    "x265-ultrafast": [
        ("libx265", "yuv420p", {
            "x265-params": "lossless=1:bframes=0:ref=1:scenecut=0",
            "preset": "ultrafast", "tune": "zerolatency", "g": "240",
        }),
    ],
    "x265-medium": [
        ("libx265", "yuv420p", {
            "x265-params": "lossless=1", "preset": "medium", "g": "240",
        }),
    ],
}

TIERS: dict[str, list[EncoderCandidate]] = {
    "lossless-ffv1": [
        # 24 slices: FFV1 decodes one thread per slice, and the client must
        # keep up at 4K — default slice counts capped decode at ~4 threads.
        ("ffv1", "yuv420p", {"level": "3", "slicecrc": "1", "slices": "24"}),
    ],
    "lossless-hevc": LOSSLESS_HEVC_PROFILES[DEFAULT_LOSSLESS_HEVC_PROFILE],
}

for _qp in (2, 4, 6, 10, 14, 18):
    TIERS[f"hevc-qp{_qp}"] = [
        ("hevc_nvenc", "yuv420p", {
            "rc": "constqp", "qp": str(_qp), "preset": "p7", "g": "240",
        }),
    ]

LEGACY_TIER_ALIASES = {"visually-lossless": "hevc-qp18"}

_PROBE_LOCK = threading.Lock()
_PROBE_SUCCESSES: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()


def _probe_key(codec: str, pix_fmt: str, options: dict[str, str]):
    return codec, pix_fmt, tuple(sorted(options.items()))


def _encoder_failure_category(error: BaseException) -> str:
    text = str(error).lower()
    if any(term in text for term in (
        "no free", "resource", "session", "out of memory", "too many",
    )):
        return "session/resource exhaustion"
    if any(term in text for term in ("option", "invalid argument", "not supported")):
        return "unsupported options"
    if any(term in text for term in ("not found", "unknown encoder", "driver", "library")):
        return "missing encoder/driver"
    if any(term in text for term in ("device", "cuda", "nvenc", "hardware")):
        return "device/driver failure"
    return "encoder open failure"


def _probe_encoder_or_raise(
    codec: str, options: dict[str, str], width: int = 256, height: int = 256,
    pix_fmt: str = "yuv420p",
) -> None:
    """Open one probe context and drop its final reference deterministically."""
    ctx = None
    try:
        ctx = av.CodecContext.create(codec, "w")
        ctx.width = width
        ctx.height = height
        ctx.pix_fmt = pix_fmt
        ctx.time_base = Fraction(1, 30)
        ctx.options = options
        ctx.open()
    finally:
        # PyAV exposes no CodecContext.close(). In CPython this final reference
        # drop immediately releases the underlying AVCodecContext/NVENC slot.
        ctx = None


def probe_encoder(codec: str, options: dict[str, str], width: int = 256, height: int = 256,
                  pix_fmt: str = "yuv420p") -> bool:
    """Can this encoder actually open here? (Codec present AND hardware usable.)"""
    try:
        _probe_encoder_or_raise(codec, options, width, height, pix_fmt)
        return True
    except Exception:
        return False


def select_encoder(
    tier: str,
    lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE,
) -> tuple[str, str, dict[str, str]]:
    requested_tier = tier
    tier = LEGACY_TIER_ALIASES.get(tier, tier)
    if tier not in TIERS:
        raise ValueError(f"unknown quality tier '{tier}' (choose from {sorted(TIERS)})")
    if lossless_hevc_profile not in LOSSLESS_HEVC_PROFILES:
        raise ValueError(
            f"unknown lossless HEVC profile {lossless_hevc_profile!r} "
            f"(choose from {sorted(LOSSLESS_HEVC_PROFILES)})"
        )
    candidates = (
        LOSSLESS_HEVC_PROFILES[lossless_hevc_profile]
        if tier == "lossless-hevc" else TIERS[tier]
    )
    failures: list[tuple[str, str, BaseException]] = []
    for codec, pix_fmt, options in candidates:
        key = _probe_key(codec, pix_fmt, options)
        try:
            with _PROBE_LOCK:
                if key not in _PROBE_SUCCESSES:
                    _probe_encoder_or_raise(codec, options, pix_fmt=pix_fmt)
                    _PROBE_SUCCESSES.add(key)
        except Exception as err:
            failures.append((codec, _encoder_failure_category(err), err))
            continue
        else:
            profile_text = (
                f" profile '{lossless_hevc_profile}'" if tier == "lossless-hevc" else ""
            )
            print(
                f"encode: tier '{requested_tier}'{profile_text} -> {codec} {options}",
                file=sys.stderr,
            )
            return codec, pix_fmt, dict(options)
    tried = [c for c, _, _ in candidates]
    profile_text = (
        f", profile '{lossless_hevc_profile}'" if tier == "lossless-hevc" else ""
    )
    detail = "; ".join(
        f"{codec} [{category}]: {error!r}"
        for codec, category, error in failures
    )
    raise RuntimeError(
        f"no encoder available for tier '{requested_tier}'{profile_text} "
        f"(tried: {tried}); {detail}"
    ) from (failures[-1][2] if failures else None)
