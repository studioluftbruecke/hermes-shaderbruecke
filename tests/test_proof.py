"""The real proof: render the exported material module in the offline three harness and compare.

Marked `chromium`: it needs a browser, so `scripts/check.sh` skips it on hosts without one. The PR
records which host ran it (Chromium 145 / SwiftShader, 2026-11-08).
"""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import exporting as EXPORTING  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import header as HEADER  # noqa: E402
from shaderbruecke.exporters import three as THREE  # noqa: E402
from shaderbruecke.exporters import tsl as TSL  # noqa: E402


def record_for(fixture: str, name: str) -> dict:
    source = (FIXTURES / fixture).read_text(encoding="utf-8")
    return {"id": "20261108-000000-000000-test", "version": 1, "name": name, "source": source,
            "sliders": RECORD.parse_sliders(source), "slider_values": {}, "channels": [],
            "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
            "checks": {"status": "ok"}, "edited_by": "agent",
            "created_at": "2026-11-08T00:00:00Z", "files": {}}


@pytest.mark.chromium
@pytest.mark.parametrize("fixture,name", [("sky.glsl", "golden-hour sky"),
                                          ("raymarch.glsl", "ice block"),
                                          ("sliders.glsl", "striped plasma")])
def test_three_export_matches_the_source_frame(fixture, name, tmp_path):
    document = record_for(fixture, name)
    files = THREE.files(document, {"credit": True})
    proof = EXPORTING.prove_three(document, files, tmp_path)
    assert proof["verdict"] == "match", proof
    assert proof["diff"]["mean"] <= PLUGIN.PROOF_MEAN_LIMIT
    assert proof["diff"]["p99"] <= PLUGIN.PROOF_P99_LIMIT
    assert len(proof["frames"]) == 2
    for path in proof["frames"]:
        assert pathlib.Path(path).is_file() and pathlib.Path(path).stat().st_size > 1000


@pytest.mark.chromium
def test_a_broken_export_is_reported_not_passed(tmp_path):
    document = record_for("sky.glsl", "golden-hour sky")
    files = THREE.files(document, {"credit": True})
    key = f"{HEADER.pascal(document['name'])}Material.js"
    files[key] = files[key].replace("void main()", "void main_broken()")
    proof = EXPORTING.prove_three(document, files, tmp_path)
    assert proof["verdict"] == "not_proven", proof
    assert proof["why"]


@pytest.mark.chromium
@pytest.mark.parametrize("fixture,name", [("sky.glsl", "golden-hour sky"),
                                          ("raymarch.glsl", "ice block"),
                                          ("sliders.glsl", "striped plasma")])
def test_tsl_export_matches_the_source_frame(fixture, name, tmp_path):
    document = record_for(fixture, name)
    raw = EXPORTING.raw_transpile(document, tmp_path / "raw")
    assert raw.get("text"), raw
    files = TSL.files(document, {"credit": True, "raw": raw["text"]})
    proof = EXPORTING.prove_tsl(document, files, tmp_path)
    assert proof["verdict"] == "match", proof
    assert proof["diff"]["mean"] <= PLUGIN.PROOF_MEAN_LIMIT
    assert proof["diff"]["p99"] <= PLUGIN.PROOF_P99_LIMIT
    assert len(proof["frames"]) == 2


@pytest.mark.chromium
def test_tsl_module_evaluates_on_its_own(tmp_path):
    """The module imports and builds a node without the proof renderer: no unresolved identifiers."""
    document = record_for("sliders.glsl", "striped plasma")
    raw = EXPORTING.raw_transpile(document, tmp_path / "raw")
    files = TSL.files(document, {"credit": True, "raw": raw["text"]})
    result = EXPORTING.harness(
        "const src = " + repr(list(files.values())[0]) + ";"
        " const url = URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));"
        " try { const mod = await import(url); const node = mod.materialNode();"
        " return { status: 'ok', exports: Object.keys(mod), has_node: !!node }; }"
        " catch (error) { return { status: 'error', detail: String(error) }; }"
        " finally { URL.revokeObjectURL(url); }", tmp_path / "eval")
    assert result.get("status") == "ok", result
    assert result.get("has_node") is True
    assert {"frequency", "warp", "palette", "materialNode"} <= set(result.get("exports") or [])


@pytest.mark.chromium
def test_tsl_matrix_constructor_matches_glsl(tmp_path):
    """GLSL mat2 takes columns, three's constructor takes rows: without the transpose it is 177/255."""
    document = {"id": "t", "version": 1, "name": "rotation", "sliders": [], "slider_values": {},
                "channels": [], "created_at": "x", "files": {},
                "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
                "checks": {"status": "ok"}, "edited_by": "agent",
                "source": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                          "  vec2 v = fragCoord / iResolution.xy - 0.5;\n"
                          "  vec2 q = mat2(0.80, 0.60, -0.60, 0.80) * v;\n"
                          "  fragColor = vec4(q * 4.0 + 0.5, 0.0, 1.0);\n}"}
    raw = EXPORTING.raw_transpile(document, tmp_path / "raw")
    assert raw.get("text"), raw
    files = TSL.files(document, {"credit": True, "raw": raw["text"]})
    proof = EXPORTING.prove_tsl(document, files, tmp_path)
    assert proof["verdict"] == "match", proof
