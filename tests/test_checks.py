"""Frame statistics and the flash verdict, on synthetic luminance sequences."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_checks():
    spec = importlib.util.spec_from_file_location("shaderbruecke_checks", ROOT / "checks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECKS = load_checks()


def square_wave(fps: float, hz: float, frames: int = 61, low: float = 0.0, high: float = 1.0):
    period = fps / hz
    return [high if (i % period) < period / 2 else low for i in range(frames)]


def test_frame_stats_mean_and_std():
    mean, std = CHECKS.frame_stats([0.2, 0.2, 0.2])
    assert round(mean, 6) == 0.2
    assert round(std, 6) == 0.0
    mean, std = CHECKS.frame_stats([0.0, 1.0])
    assert round(mean, 6) == 0.5
    assert round(std, 6) == 0.5


def test_flat_detects_black_and_single_colour():
    black = [{"luma_mean": 0.0, "luma_std": 0.0}, {"luma_mean": 0.0, "luma_std": 0.0}]
    grey = [{"luma_mean": 0.5, "luma_std": 0.0}]
    textured = [{"luma_mean": 0.5, "luma_std": 0.2}]
    assert CHECKS.flat(black) is True
    assert CHECKS.flat(grey) is True
    assert CHECKS.flat(textured) is False
    assert CHECKS.flat(textured + black) is False


def test_two_hertz_flicker_is_none():
    verdict = CHECKS.flash_verdict(square_wave(60, 2))
    assert verdict["verdict"] == "none", verdict


def test_five_hertz_flicker_blocks():
    verdict = CHECKS.flash_verdict(square_wave(60, 5))
    assert verdict["verdict"] == "block", verdict


def test_exactly_three_flashes_warns():
    # Six opposing transitions inside one second: three completed flashes.
    frames = ([1.0] * 10 + [0.0] * 5 + [1.0] * 10 + [0.0] * 10 + [1.0] * 10 + [0.0] * 10 + [1.0] * 6)
    assert len(frames) == 61
    verdict = CHECKS.flash_verdict(frames)
    assert verdict["max_general_per_s"] == 3, verdict
    assert verdict["verdict"] == "warn", verdict


def test_slow_fade_is_not_a_flash():
    # A 0.5 s ramp each way never has a 10% step inside one frame.
    frames = [i / 50 for i in range(0, 25)] + [1 - i / 50 for i in range(0, 25)] + [0.0] * 10
    assert CHECKS.flash_verdict(frames)["verdict"] == "none"


def test_bright_states_do_not_count():
    # Changes between 0.85 and 1.0 are fast but the darker state is above 0.80: no general flash.
    frames = square_wave(60, 5, low=0.85, high=1.0)
    assert CHECKS.flash_verdict(frames)["verdict"] == "none"


def test_red_share_never_returns_none():
    lumas = [0.5] * 60
    reds = [0.0] * 30 + [0.6] * 30
    verdict = CHECKS.flash_verdict(lumas, reds)
    assert verdict["red"] is True
    assert verdict["verdict"] == "warn"


def test_empty_sequence_is_none():
    assert CHECKS.flash_verdict([])["verdict"] == "none"
