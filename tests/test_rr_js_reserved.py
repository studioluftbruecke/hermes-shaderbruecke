"""Slider names that are JavaScript reserved words never reach the R3F and TSL files.

GLSL accepts `new`, `delete` or `function` as identifiers, but `{ new = 0.5 }` in a .tsx and
`export const new = ...` in a .tsl.js do not parse. The parser ignores such a slider line (as it
does for GLSL keywords), and an export of a version stored before that rule refuses instead of
writing files that cannot load.
"""

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from shaderbruecke import exporting, record  # noqa: E402
from shaderbruecke.exporters import registry  # noqa: E402

WORDS = ["new", "delete", "function", "class", "await", "yield", "let", "eval", "arguments", "typeof"]


@pytest.mark.parametrize("word", WORDS)
def test_the_parser_ignores_a_slider_named_like_a_javascript_reserved_word(word):
    source = f"// @slider {word} 0.0..1.0 = 0.5\n// @slider glow 0.0..1.0 = 0.5\n"
    assert [s["name"] for s in record.parse_sliders(source)] == ["glow"]


@pytest.fixture
def stored_with_new(tmp_path, monkeypatch):
    """A version stored before the rule: its record carries a slider named `new`."""
    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    shader_id = sb_fakes.new_shader(db, base)
    conn = sqlite3.connect(db)
    doc = record.latest(conn, shader_id)
    doc["sliders"] = [{**doc["sliders"][0], "name": "new"}]
    assert record.update(conn, doc)
    return conn, base, shader_id


@pytest.mark.parametrize("target", ["r3f", "tsl"])
def test_an_export_of_such_a_stored_version_refuses_and_writes_nothing(stored_with_new, target, monkeypatch):
    conn, base, shader_id = stored_with_new
    monkeypatch.setattr(exporting, "prove", lambda *a, **k: pytest.fail("the proof must not run"))
    answer = exporting.export_shader(conn, base, shader_id, target, settings=dict(sb_fakes.SETTINGS))
    assert answer["success"] is False
    assert "`new`" in answer["error"] and "JavaScript reserved word" in answer["error"]
    assert not exporting.export_dir(base, shader_id, 1, target).exists()


def test_targets_that_do_not_use_the_name_as_a_javascript_identifier_still_export(stored_with_new, monkeypatch):
    conn, base, shader_id = stored_with_new
    monkeypatch.setattr(exporting, "prove", lambda *a, **k: {"method": "fake", "verdict": "match", "why": "",
                                                             "checks": [], "diff": None, "frames": []})
    answer = exporting.export_shader(conn, base, shader_id, "three", settings=dict(sb_fakes.SETTINGS))
    assert answer["success"] is True, answer


def test_the_r3f_proof_does_not_claim_a_render_of_the_tsx_wrapper(monkeypatch, tmp_path):
    monkeypatch.setattr(exporting, "prove_three", lambda *a, **k: {"method": "three-harness", "verdict": "match",
                                                                   "why": "rendered", "checks": [], "frames": []})
    proof = exporting.prove({}, registry.EXPORT_TARGETS["r3f"], {}, tmp_path)
    assert "app render" not in proof["why"] and "proven by" not in proof["why"]
    assert ".tsx" in proof["why"]
