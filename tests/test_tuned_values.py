"""The human's slider values flow end to end: record, every exporter, the handoff, the proof and
the tool results. Plus the optional human label on an `@slider` line.
"""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
RAW_SKY = (FIXTURES / "expected" / "sky.transpiler-raw.txt").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import registry as REGISTRY  # noqa: E402

SKY = (FIXTURES / "sky.glsl").read_text(encoding="utf-8")


def sky(**extra):
    doc = {"id": "sky", "version": 1, "name": "golden-hour sky", "source": SKY,
           "sliders": RECORD.parse_sliders(SKY), "channels": [],
           "provenance": {"origin": "agent", "url": None, "author": None, "licence": None,
                          "parent": None},
           "checks": {"status": "ok"}, "edited_by": "agent",
           "created_at": "2026-11-08T00:00:00Z", "files": {}}
    doc.update(extra)
    return doc


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "data.db")
    yield connection
    connection.close()


def test_effective_values_are_tuned_else_authored():
    assert RECORD.effective_values(sky()) == {"sunHeight": 0.32, "cloudCover": 0.55, "warmth": 0.75}
    tuned = sky(tuned_values={"sunHeight": 0.41})
    assert RECORD.effective_values(tuned) == {"sunHeight": 0.41, "cloudCover": 0.55, "warmth": 0.75}
    legacy = sky(slider_values={"warmth": 0.2})          # rows written before tuned_values existed
    assert RECORD.effective_values(legacy)["warmth"] == 0.2


def test_set_slider_values_merges_known_finite_clamped_numbers(conn):
    RECORD.save(conn, sky())
    updated = RECORD.set_slider_values(conn, "sky", {"sunHeight": 0.41, "nope": 0.5})
    assert updated["tuned_values"] == {"sunHeight": 0.41}
    assert updated["edited_by"] == "human"
    again = RECORD.set_slider_values(conn, "sky", {"warmth": 7.0, "cloudCover": float("nan"),
                                                  "sunHeight": True})
    assert again["tuned_values"] == {"sunHeight": 0.41, "warmth": 1.0}
    assert RECORD.latest(conn, "sky")["tuned_values"] == {"sunHeight": 0.41, "warmth": 1.0}
    assert RECORD.set_slider_values(conn, "missing", {"warmth": 0.1}) is None


def test_reset_returns_to_the_authored_defaults(conn):
    RECORD.save(conn, sky())
    RECORD.set_slider_values(conn, "sky", {"sunHeight": 0.41})
    reset = RECORD.reset_tuned_values(conn, "sky")
    assert reset["tuned_values"] == {}
    assert RECORD.effective_values(RECORD.latest(conn, "sky"))["sunHeight"] == 0.32
    assert RECORD.reset_tuned_values(conn, "missing") is None


def all_files(doc):
    out = {}
    for target_id, row in REGISTRY.EXPORT_TARGETS.items():
        options = {"credit": True, "raw": RAW_SKY}
        out[target_id] = row.files(doc, options)
    return out


def test_every_export_carries_the_tuned_value():
    doc = sky(tuned_values={"sunHeight": 0.41})
    files = all_files(doc)
    material = files["three"]["GoldenHourSkyMaterial.js"]
    assert "sunHeight: { value: options.sunHeight ?? 0.41 }" in material
    assert "sunHeight = 0.41," in files["r3f"]["GoldenHourSky.tsx"] or \
        "sunHeight = 0.41" in files["r3f"]["GoldenHourSky.tsx"]
    assert "const float sunHeight = 0.41;" in files["shadertoy"]["GoldenHourSky.shadertoy.glsl"]
    assert "export const sunHeight = uniform( 0.41 );" in files["tsl"]["GoldenHourSky.tsl.js"]
    handoff = files["agent"]["GoldenHourSky.handoff.md"]
    assert "sunHeight: { value: options.sunHeight ?? 0.41 }" in handoff
    assert "`sunHeight`: slider, 0.05..0.95, default 0.41 (tuned by the human; authored 0.32)." in handoff
    for target_id, target_files in files.items():
        for filename, text in target_files.items():
            assert "sunHeight 0.05..0.95 (default 0.32, tuned 0.41, step 0.01)" in text, filename
            assert "cloudCover 0..1 (default 0.55, step 0.01)" in text, filename


def test_r3f_props_default_to_the_tuned_values():
    from shaderbruecke.exporters import r3f

    text = r3f.component(sky(tuned_values={"warmth": 0.2}))
    assert "warmth = 0.2" in text and "sunHeight = 0.32" in text


def test_library_get_names_tuned_and_effective_values(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), tmp_path / "data"))
    connection = sqlite3.connect(db)
    RECORD.save(connection, sky())
    RECORD.set_slider_values(connection, "sky", {"warmth": 0.2})
    connection.close()
    got = json.loads(PLUGIN.shader_library({"action": "get", "id": "sky"}))["shader"]
    assert got["tuned_values"] == {"warmth": 0.2}
    assert got["effective_values"] == {"sunHeight": 0.32, "cloudCover": 0.55, "warmth": 0.2}


def test_slider_lines_may_carry_a_human_label():
    sliders = RECORD.parse_sliders(
        '// @slider sunHeight 0.05..0.95 = 0.32 step 0.01 "Sun height"\n'
        '// @slider glow 0.0..1.0 = 0.5 "Glow amount"\n'
        "// @slider plain 0.0..1.0 = 0.5\n"
        '// @slider broken 0.0..1.0 = 0.5 "unterminated\n')
    assert [(s["name"], s.get("label")) for s in sliders] == \
        [("sunHeight", "Sun height"), ("glow", "Glow amount"), ("plain", None)]
    assert sliders[0]["step"] == 0.01


@pytest.mark.chromium
def test_the_proof_renders_at_the_effective_values(tmp_path):
    from shaderbruecke import exporting

    doc = sky(tuned_values={"sunHeight": 0.41})
    files = REGISTRY.EXPORT_TARGETS["three"].files(doc, {"credit": True})
    proof = exporting.prove(doc, REGISTRY.EXPORT_TARGETS["three"], files, tmp_path)
    assert proof["verdict"] == "match", proof
    assert proof["checks"] and proof["checks"][0]["values"]["sunHeight"] == 0.41


@pytest.mark.chromium
def test_a_new_version_names_the_values_the_human_tuned_on_the_last_one(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    first = json.loads(PLUGIN.shader_render({"source": SKY, "name": "golden-hour sky"}))
    assert first["success"] is True and first["tuned_values"] == {}
    connection = sqlite3.connect(db)
    RECORD.set_slider_values(connection, first["id"], {"sunHeight": 0.41})
    connection.close()
    second = json.loads(PLUGIN.shader_render({"source": SKY, "id": first["id"]}))
    assert second["success"] is True and second["version"] == 2
    assert second["tuned_values"] == {}
    assert second["previous_tuned_values"] == {"sunHeight": 0.41}
    assert "0.41" in second["next"]
