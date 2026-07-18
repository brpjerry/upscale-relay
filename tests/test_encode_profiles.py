import os

import pytest

import upscale_cli.encode as encode


def test_low_delay_profiles_are_decoder_simple_and_long_gop():
    for name in ("nvenc-p1-low-delay", "nvenc-p4-low-delay"):
        codec, pix_fmt, options = encode.LOSSLESS_HEVC_PROFILES[name][0]
        assert codec == "hevc_nvenc"
        assert pix_fmt == "yuv420p"
        assert options["tune"] == "lossless"
        assert options["bf"] == "0"
        assert options["refs"] == "1"
        assert options["rc-lookahead"] == "0"
        assert options["g"] == "240"


def test_software_profiles_force_libx265():
    fast = encode.LOSSLESS_HEVC_PROFILES["x265-ultrafast"][0]
    compact = encode.LOSSLESS_HEVC_PROFILES["x265-medium"][0]
    assert fast[0] == compact[0] == "libx265"
    assert fast[2]["preset"] == "ultrafast"
    assert "bframes=0" in fast[2]["x265-params"]
    assert compact[2]["preset"] == "medium"


def test_slice_profiles_only_change_slice_count():
    control = encode.LOSSLESS_HEVC_PROFILES["nvenc-p7"][0][2]
    for name, count in (("nvenc-p7-slices4", "4"), ("nvenc-p7-slices8", "8")):
        codec, pix_fmt, options = encode.LOSSLESS_HEVC_PROFILES[name][0]
        assert codec == "hevc_nvenc"
        assert pix_fmt == "yuv420p"
        assert {key: value for key, value in options.items() if key != "slices"} == control
        assert options["slices"] == count


def test_nvenc_qp_ladder_is_constant_qp_and_not_lossless_tuned():
    for qp in (0, 2, 4, 6, 8):
        codec, pix_fmt, options = encode.LOSSLESS_HEVC_PROFILES[f"nvenc-p7-qp{qp}"][0]
        assert codec == "hevc_nvenc"
        assert pix_fmt == "yuv420p"
        assert options["rc"] == "constqp"
        assert options["qp"] == str(qp)
        assert options["preset"] == "p7"
        assert options["g"] == "240"
        assert "tune" not in options


def test_select_encoder_uses_requested_profile(monkeypatch):
    monkeypatch.setattr(encode, "probe_encoder", lambda *args, **kwargs: True)
    codec, _, options = encode.select_encoder(
        "lossless-hevc", lossless_hevc_profile="nvenc-p4-low-delay",
    )
    assert codec == "hevc_nvenc"
    assert options["preset"] == "p4"
    assert options["bf"] == "0"


def test_select_encoder_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unknown lossless HEVC profile"):
        encode.select_encoder("lossless-hevc", lossless_hevc_profile="unknown")


def test_public_lossy_options_map_to_requested_nvenc_qps():
    lossy = [option for option in encode.QUALITY_OPTIONS if not option["lossless"]]
    assert [option["p95_mbps"] for option in lossy] == [350, 250, 200, 100, 50, 50]
    for qp in (2, 4, 6, 10, 14, 18):
        codec, pix_fmt, options = encode.TIERS[f"hevc-qp{qp}"][0]
        assert (codec, pix_fmt) == ("hevc_nvenc", "yuv420p")
        assert options["qp"] == str(qp)
        assert options["preset"] == "p7"


def test_lossless_hevc_tier_wires_through_the_effective_default():
    assert (
        encode.TIERS["lossless-hevc"]
        is encode.LOSSLESS_HEVC_PROFILES[encode.DEFAULT_LOSSLESS_HEVC_PROFILE]
    )


def test_shipped_default_is_p4_low_delay():
    if "RELAY_LOSSLESS_HEVC_PROFILE" in os.environ:
        pytest.skip("default profile overridden by RELAY_LOSSLESS_HEVC_PROFILE")
    assert encode.DEFAULT_LOSSLESS_HEVC_PROFILE == "nvenc-p4-low-delay"
    assert encode.TIERS["lossless-hevc"][0][2]["preset"] == "p4"
