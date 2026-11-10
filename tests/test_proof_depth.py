"""Proof that earns every verdict: three moments, two slider sets, a rendered Shadertoy tab, the
handoff's embedded module, and a host-free acceptance check.

The `chromium` tests need a browser; `scripts/check.sh` skips them on hosts without one.
"""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import exporting as EXPORTING  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import handoff as HANDOFF  # noqa: E402
from shaderbruecke.exporters import registry as REGISTRY  # noqa: E402
from shaderbruecke.exporters import shadertoy as SHADERTOY  # noqa: E402
from shaderbruecke.exporters import three as THREE  # noqa: E402


def record_for(fixture: str, name: str, shader_id: str = "fixture", **extra) -> dict:
    source = (FIXTURES / fixture).read_text(encoding="utf-8")
    doc = {"id": shader_id, "version": 1, "name": name, "source": source,
           "sliders": RECORD.parse_sliders(source), "channels": [],
           "provenance": {"origin": "agent", "url": None, "author": None, "licence": None,
                          "parent": None},
           "checks": {"status": "ok"}, "edited_by": "agent",
           "created_at": "2026-11-08T00:00:00Z", "files": {}}
    doc.update(extra)
    return doc


def quarter(doc):
    return {s["name"]: s["min"] + 0.25 * (s["max"] - s["min"]) for s in doc["sliders"]}


# Units, no browser --------------------------------------------------------------------------------

def test_shadertoy_constants_are_glsl_float_literals():
    text = SHADERTOY.source_tab(record_for("sliders.glsl", "striped plasma"))
    assert "const float frequency = 5.0;" in text          # `= 5;` is an int: a compile error
    assert "const float warp = 0.8;" in text


def test_shadertoy_tab_keeps_the_source_byte_for_byte():
    doc = record_for("sky.glsl", "golden-hour sky")
    doc["source"] = "// a\n\n\n\n// b\n" + doc["source"]
    assert doc["source"].strip("\n") in SHADERTOY.source_tab(doc)


def test_the_handoff_names_no_host_path(tmp_path):
    doc = record_for("sky.glsl", "golden-hour sky")
    doc["files"] = {"snapshot": str(tmp_path / "frame-t1.png"),
                    "contact_sheet": str(tmp_path / "contact-sheet.png")}
    text = HANDOFF.handoff(doc)
    assert str(tmp_path) not in text
    assert "reference-t1.png" in text


def test_the_handoff_states_the_grid_it_must_match():
    doc = record_for("sky.glsl", "golden-hour sky")
    grid = [[10.0 * i, 20.0, 30.5] for i in range(9)]
    text = HANDOFF.handoff(doc, acceptance={"grid": grid, "png": "reference-t1.png"})
    assert "3x3" in text and "t = 1 s" in text
    assert "| 1 | top left | 0.0 | 20.0 | 30.5 |" in text
    assert "| 9 | bottom right | 80.0 | 20.0 | 30.5 |" in text
    assert f"{HANDOFF.ACCEPT_LIMIT:g}/255" in text


# Chromium -------------------------------------------------------------------------------------------

@pytest.mark.chromium
def test_the_proof_checks_three_moments_and_two_slider_sets(tmp_path):
    doc = record_for("sky.glsl", "golden-hour sky", tuned_values={"sunHeight": 0.41})
    files = THREE.files(doc, {"credit": True})
    proof = EXPORTING.prove(doc, REGISTRY.EXPORT_TARGETS["three"], files, tmp_path)
    assert proof["verdict"] == "match", proof
    seen = {(c["t"], json.dumps(c["values"], sort_keys=True)) for c in proof["checks"]}
    effective = RECORD.effective_values(doc)
    expected = {(t, json.dumps(values, sort_keys=True))
                for t in (0.5, 1.0, 2.0) for values in (effective, quarter(doc))}
    assert seen == expected
    assert proof["diff"]["mean"] == max(c["mean"] for c in proof["checks"])
    assert proof["diff"]["p99"] == max(c["p99"] for c in proof["checks"])
    assert len(proof["grid"]) == 9 and all(len(cell) == 3 for cell in proof["grid"])


@pytest.mark.chromium
def test_an_unwired_slider_is_reported_as_differs(tmp_path):
    doc = record_for("sliders.glsl", "striped plasma")
    files = THREE.files(doc, {"credit": True})
    key = "StripedPlasmaMaterial.js"
    assert "frequency: { value: options.frequency ?? 5 }" in files[key]
    files[key] = files[key].replace("frequency: { value: options.frequency ?? 5 }",
                                    "frequency: { value: 5 }")
    proof = EXPORTING.prove(doc, REGISTRY.EXPORT_TARGETS["three"], files, tmp_path)
    assert proof["verdict"] == "differs", proof
    failing = [c for c in proof["checks"] if c["mean"] > EXPORTING.PROOF_MEAN_LIMIT
               or c["p99"] > EXPORTING.PROOF_P99_LIMIT]
    assert failing and all(c["values"] == quarter(doc) for c in failing)


@pytest.mark.chromium
def test_a_time_offset_is_reported_as_differs(tmp_path):
    doc = record_for("sliders.glsl", "striped plasma")
    files = THREE.files(doc, {"credit": True})
    key = "StripedPlasmaMaterial.js"
    files[key] = files[key].replace("uniforms.iTime.value = seconds",
                                    "uniforms.iTime.value = seconds + 0.5")
    proof = EXPORTING.prove(doc, REGISTRY.EXPORT_TARGETS["three"], files, tmp_path)
    assert proof["verdict"] == "differs", proof
    assert {c["t"] for c in proof["checks"]} == {0.5, 1.0, 2.0}


@pytest.mark.chromium
@pytest.mark.parametrize("fixture,name", [("sky.glsl", "golden-hour sky"),
                                          ("raymarch.glsl", "ice block"),
                                          ("sliders.glsl", "striped plasma")])
def test_the_shadertoy_tab_is_rendered_and_matches(fixture, name, tmp_path):
    doc = record_for(fixture, name)
    files = SHADERTOY.files(doc, {"credit": True})
    proof = EXPORTING.prove(doc, REGISTRY.EXPORT_TARGETS["shadertoy"], files, tmp_path)
    assert proof["method"] == "shadertoy-harness"
    assert proof["verdict"] == "match", proof
    assert {c["t"] for c in proof["checks"]} == {0.5, 1.0, 2.0}


@pytest.mark.chromium
def test_a_shadertoy_tab_that_drifts_is_caught(tmp_path):
    doc = record_for("sliders.glsl", "striped plasma")
    files = SHADERTOY.files(doc, {"credit": True})
    key = "StripedPlasma.shadertoy.glsl"
    files[key] = files[key].replace("const float frequency = 5.0;", "const float frequency = 7.0;")
    proof = EXPORTING.prove(doc, REGISTRY.EXPORT_TARGETS["shadertoy"], files, tmp_path)
    assert proof["verdict"] == "differs", proof


@pytest.mark.chromium
def test_the_agent_handoff_is_proven_on_its_embedded_module(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    connection = sqlite3.connect(db)
    RECORD.save(connection, record_for("sky.glsl", "golden-hour sky", shader_id="sky"))
    connection.close()
    result = json.loads(PLUGIN.shader_export({"id": "sky", "target": "agent"}))
    assert result["success"] is True, result
    proof = result["proof"]
    assert proof["verdict"] == "match", proof
    assert "embedded" in proof["method"] and "embedded" in proof["why"]
    names = sorted(pathlib.Path(f["path"]).name for f in result["files"])
    assert names == ["GoldenHourSky.handoff.md", "reference-t1.png"]
    folder = pathlib.Path(result["files"][0]["path"]).parent
    assert (folder / "reference-t1.png").stat().st_size > 1000
    text = (folder / "GoldenHourSky.handoff.md").read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    first = proof["grid"][0]
    assert f"| 1 | top left | {first[0]:.1f} | {first[1]:.1f} | {first[2]:.1f} |" in text
    stored = EXPORTING.read_proof(base, "sky", 1, "agent")
    assert stored["verdict"] == "match" and stored["checks"] == proof["checks"]
