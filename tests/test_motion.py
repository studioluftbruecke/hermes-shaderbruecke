"""F25 (K-S1, 2026-11-08): the render result must say whether the frames move.

The K-S1 sky drifts its clouds at 0.08 units/s. Its three frames had the same `luma_mean` and
`luma_std` to three decimals, so the agent told Konstantin the frames "look identical" and
suspected the renderer of not advancing time. Measured on those PNGs, 17.6% of pixels change from
t = 0 to 1 s. The result now carries, per frame, the share of pixels that changed against the
previous frame, and `checks.motion` turns that into `moving` or `still`.
"""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import checks as CHECKS  # noqa: E402

DRIFT = (ROOT / "tests" / "fixtures" / "ks1-drifting-clouds.glsl").read_text(encoding="utf-8")
STATIC = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
          "  vec2 uv = fragCoord / iResolution.xy;\n  fragColor = vec4(uv, 0.5, 1.0);\n}\n")


def test_motion_is_still_when_no_frame_changed():
    frames = [{"t": 0.0, "moved": None}, {"t": 1.0, "moved": 0.0}, {"t": 2.5, "moved": 0.0004}]
    assert CHECKS.motion(frames) == {"verdict": "still", "moved": [0.0, 0.0004], "max": 0.0004}


def test_motion_is_moving_when_a_frame_changed_enough():
    frames = [{"t": 0.0, "moved": None}, {"t": 1.0, "moved": 0.176}, {"t": 2.5, "moved": 0.08}]
    assert CHECKS.motion(frames)["verdict"] == "moving"


def test_motion_is_unknown_without_numbers():
    assert CHECKS.motion([{"t": 0.0}, {"t": 1.0}])["verdict"] == "unknown"


def render(tmp_path, monkeypatch, source, name):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    return json.loads(PLUGIN.shader_render({"source": source, "name": name}))


@pytest.mark.chromium
def test_the_k_s1_sky_reports_moving(tmp_path, monkeypatch):
    result = render(tmp_path, monkeypatch, DRIFT, "drifting sky")
    assert result["success"] is True, result
    frames = result["checks"]["frames"]
    # Whole-frame statistics stay the same; that was the trap.
    assert len({f["luma_mean"] for f in frames}) == 1, frames
    motion = result["checks"]["motion"]
    assert motion["verdict"] == "moving", motion
    assert motion["max"] > 0.1, motion
    assert frames[0]["moved"] is None and frames[1]["moved"] > 0.1, frames


@pytest.mark.chromium
def test_a_static_shader_reports_still_and_says_so(tmp_path, monkeypatch):
    result = render(tmp_path, monkeypatch, STATIC, "static gradient")
    assert result["success"] is True, result
    assert result["checks"]["motion"]["verdict"] == "still", result["checks"]["motion"]
    assert "does not move" in result["next"], result["next"]
