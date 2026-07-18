import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from upscale_cli.fit import (
    aligned_target_dimensions,
    cover_crop_box,
    cover_dimensions,
    fit_dimensions,
    interpolation_for_algorithm,
)


def test_downscale_overshoot():
    # 2x model on 1080p -> 3840x2160, fit to a 2960x1848 panel
    w, h = fit_dimensions(3840, 2160, 2960, 1848)
    assert w <= 2960 and h <= 1848
    assert w % 2 == 0 and h % 2 == 0
    assert abs(w / h - 16 / 9) < 0.01


def test_mild_upscale():
    w, h = fit_dimensions(2560, 1440, 2960, 1848)
    assert (w, h) > (2560, 1440)
    assert w <= 2960 and h <= 1848


def test_exact_fit_passthrough():
    assert fit_dimensions(1920, 1080, 1920, 1080) == (1920, 1080)


def test_mod16_alignment():
    w, h = fit_dimensions(3840, 2160, 2960, 1848, align=16)
    assert w % 16 == 0 and h % 16 == 0
    assert w <= 2960 and h <= 1848


def test_odd_aspect_content():
    # 4:3 content on a wide panel
    w, h = fit_dimensions(1440, 1080, 2960, 1848)
    assert h <= 1848 and w <= 2960
    assert abs(w / h - 4 / 3) < 0.02


def test_cover_16x9_on_16x10():
    # 16:9 video covering a 1440x900 (16:10) screen -> 1600x900, the height
    # exact and the width overflowing for panscan to crop (docs/PROTOCOL.md example).
    w, h = cover_dimensions(3840, 2160, 1440, 900)
    assert (w, h) == (1600, 900)


def test_cover_fully_contains_target():
    # Covers on both axes for a range of source/target aspect combinations.
    for sw, sh in [(3840, 2160), (1920, 1080), (1440, 1080), (2048, 858)]:
        for tw, th in [(1440, 900), (1920, 1080), (1366, 768), (2560, 1440)]:
            w, h = cover_dimensions(sw, sh, tw, th)
            assert w >= tw and h >= th
            assert w % 2 == 0 and h % 2 == 0
            assert abs((w / h) - (sw / sh)) < 0.02


def test_cover_exact_fit_passthrough():
    assert cover_dimensions(1920, 1080, 1920, 1080) == (1920, 1080)


def test_cover_crop_box_removes_only_offscreen_sides():
    x, y, w, h = cover_crop_box(3840, 2160, 2960, 1848)
    assert y == 0 and h == 2160
    assert x > 0 and x % 2 == 0 and w % 2 == 0
    assert x * 2 + w <= 3840
    assert abs(w / h - 2960 / 1848) < 0.002


def test_cover_crop_box_removes_vertical_overflow():
    x, y, w, h = cover_crop_box(1440, 1920, 1920, 1080)
    assert x == 0 and w == 1440
    assert y > 0 and y % 2 == 0 and h % 2 == 0
    assert abs(w / h - 16 / 9) < 0.01


def test_aligned_target_and_resize_algorithm_validation():
    assert aligned_target_dimensions(1365, 767) == (1364, 766)
    assert interpolation_for_algorithm("area") == "AREA"
    assert interpolation_for_algorithm("bicublin") == "BICUBLIN"
    assert interpolation_for_algorithm("gaussian") == "GAUSS"
    assert interpolation_for_algorithm("sinc") == "SINC"
    with pytest.raises(ValueError, match="unknown resize algorithm"):
        interpolation_for_algorithm("magic")
