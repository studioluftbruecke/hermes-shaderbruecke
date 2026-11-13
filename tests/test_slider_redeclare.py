"""A `@slider` name declared again by the shader.

Every `@slider` line becomes `uniform float <name>;` before the agent's lines, on the host and in
Desktop. A shader that also declares that name itself (`const float sunHeight = 0.22;`) fails to
compile with `'sunHeight' : redefinition`. In a hands-on test the Desktop card showed that raw log under
"It compiled on the Hermes host, so this is a difference between GPUs", which was false: the
backend that rendered it was an older Shaderbrücke that never injected slider uniforms. The lint
now names the collision on both halves before any compile, and a local variable that hides a
slider (the slider then moves nothing) is a warning.
"""

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import checks as CHECKS  # noqa: E402

KS1 = (ROOT / "tests" / "fixtures" / "ks1-const-sliders.glsl").read_text(encoding="utf-8")


def rules(findings, rule):
    return [(f.line, f.message) for f in findings if f.rule == rule]


def test_the_k_s1_source_is_a_lint_error_on_the_three_declarations():
    findings = CHECKS.lint(KS1)
    hits = rules(findings, "slider-redeclare")
    assert [line for line, _ in hits] == [7, 8, 9], findings
    assert all(f.severity == "error" for f in findings if f.rule == "slider-redeclare")
    assert "`sunHeight`" in hits[0][1] and "`warmth`" in hits[2][1]


def test_uniform_and_define_count_as_declarations():
    source = ("// @slider glow 0.0..1.0 = 0.5\n// @slider tint 0.0..1.0 = 0.5\n"
              "uniform float glow;\n#define tint 0.4\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow, tint, 0.0, 1.0); }\n")
    assert [line for line, _ in rules(CHECKS.lint(source), "slider-redeclare")] == [3, 4]


def test_a_parameter_with_a_slider_name_is_fine():
    source = ("// @slider glow 0.0..1.0 = 0.5\n"
              "float halo(float glow, vec2 p) { return glow * length(p); }\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(halo(glow, fragCoord)); }\n")
    assert CHECKS.lint(source) == []


def test_a_local_that_hides_a_slider_is_a_warning():
    source = ("// @slider glow 0.0..1.0 = 0.5\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  float glow = 0.2;\n  fragColor = vec4(glow);\n}\n")
    findings = CHECKS.lint(source)
    assert [(f.rule, f.line, f.severity) for f in findings] == [("slider-shadow", 3, "warn")]


def test_a_name_in_a_comment_or_without_a_slider_line_is_not_flagged():
    source = ("// const float glow = 0.2;  (old idea)\n"
              "const float glowBase = 0.2;\nconst float other = 0.1;\n"
              "// @slider glow 0.0..1.0 = 0.5\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow + glowBase + other); }\n")
    assert CHECKS.lint(source) == []


def test_lint_uses_the_names_the_record_parsed_when_given():
    # An invalid @slider line (default outside the range) is no slider, so no uniform is injected
    # for it and declaring the name is legal.
    source = ("// @slider glow 0.0..1.0 = 7.0\nconst float glow = 0.2;\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow); }\n")
    assert CHECKS.lint(source, slider_names=[]) == []


def test_shader_render_stops_the_k_s1_source_before_the_browser(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))

    def no_browser(*args, **kwargs):
        raise AssertionError("the lint must stop this source before a browser starts")

    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", no_browser)
    result = json.loads(PLUGIN.shader_render({"source": KS1, "name": "golden-hour sky"}))
    assert result["success"] is False and result["error"].startswith("lint_error: 3 error(s)"), result
    assert "line 7: delete this declaration" in result["next"], result["next"]
