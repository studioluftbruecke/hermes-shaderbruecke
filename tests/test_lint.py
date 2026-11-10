"""Every lint rule in lint_rules.json has a sample that triggers it and one that does not.

The test loops over the JSON, so a rule added without samples fails here instead of shipping.
Comment-only samples prove that a commented-out mistake never blocks a render.
"""

import importlib.util
import json
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
RULES = json.loads((ROOT / "lint_rules.json").read_text(encoding="utf-8"))["rules"]

# One triggering and one clean sample per rule id. The clean sample must trigger nothing at all.
SAMPLES = {
    "while-true": {
        "bad": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  while (true) { fragColor = vec4(1.0); }\n}",
        "good": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  for (int i = 0; i < 8; i++) { fragColor = vec4(0.1); }\n}",
    },
    "for-bound": {
        "bad": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  float acc = 0.0;\n"
               "  for (int i = 0; i < steps; i++) { acc += 0.1; }\n"
               "  fragColor = vec4(acc);\n}",
        "good": "const int STEPS = 8;\n"
                "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  float acc = 0.0;\n"
                "  for (int i = 0; i < STEPS; i++) { acc += 0.1; }\n"
                "  fragColor = vec4(acc);\n}",
    },
    "float-counter": {
        "bad": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  for (float i = 0.0; i < 1.0; i += 0.1) { fragColor = vec4(i); }\n}",
        "good": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  for (int i = 0; i < 10; i++) { fragColor = vec4(float(i)); }\n}",
    },
    "webgl1": {
        "bad": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  gl_FragColor = vec4(1.0);\n}",
        "good": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  fragColor = vec4(texture(iChannel0, fragCoord).rgb, 1.0);\n}",
    },
    "redeclare": {
        "bad": "uniform float iTime;\n"
               "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  fragColor = vec4(iTime);\n}",
        "good": "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  fragColor = vec4(iTime, 0.0, 0.0, 1.0);\n}",
    },
    "own-main": {
        "bad": "void main() { }\n"
               "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }",
        "good": "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }",
    },
    "slider-redeclare": {
        "bad": "// @slider glow 0.0..1.0 = 0.3 step 0.01\n"
               "const float glow = 0.3;\n"
               "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n  fragColor = vec4(glow);\n}",
        "good": "// @slider glow 0.0..1.0 = 0.3 step 0.01\n"
                "float halo(float glow) { return glow * 0.5; }\n"
                "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n  fragColor = vec4(halo(glow));\n}",
    },
    "slider-shadow": {
        "bad": "// @slider glow 0.0..1.0 = 0.3\n"
               "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
               "  float glow = 0.2;\n  fragColor = vec4(glow);\n}",
        "good": "// @slider glow 0.0..1.0 = 0.3\n"
                "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                "  float g = glow * 0.5;\n  fragColor = vec4(g);\n}",
    },
}


def test_every_rule_has_samples():
    ids = [rule["id"] for rule in RULES]
    assert len(ids) == len(set(ids)), "duplicate rule id"
    assert sorted(ids) == sorted(SAMPLES), "a rule without samples, or a sample without a rule"


def test_every_rule_triggers_on_its_own_sample():
    for rule in RULES:
        findings = CHECKS.lint(SAMPLES[rule["id"]]["bad"])
        hits = [f for f in findings if f.rule == rule["id"]]
        assert hits, f"{rule['id']} did not fire on its sample"
        assert hits[0].severity == rule["severity"]


def test_clean_samples_are_clean():
    for rule in RULES:
        findings = CHECKS.lint(SAMPLES[rule["id"]]["good"])
        assert findings == [], f"{rule['id']} clean sample raised {findings}"


def test_comments_never_block_a_render():
    source = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  // while (true) is fine in a comment; so is gl_FragColor\n"
              "  /* for (float i = 0.0; i < 1.0; i += 0.1) { }\n"
              "     uniform float iTime; */\n"
              "  fragColor = vec4(1.0);\n}")
    assert CHECKS.lint(source) == []


def test_comment_stripping_keeps_line_numbers():
    source = ("/* one\n two */\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  gl_FragColor = vec4(1.0);\n}")
    findings = CHECKS.lint(source)
    assert [(f.rule, f.line) for f in findings] == [("webgl1", 4)]


def test_for_bound_accepts_constants_and_literals():
    source = ("const int OCTAVES = 4;\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  float sum = 0.0;\n"
              "  for (int i = 0; i < OCTAVES; ++i) { sum += 0.25; }\n"
              "  for (int j = OCTAVES; j > 0; --j) { sum += 0.25; }\n"
              "  for (int k = 0; k <= 3; k++) { sum += 0.25; }\n"
              "  fragColor = vec4(sum);\n}")
    assert CHECKS.lint(source) == []


def test_for_bound_rejects_a_runtime_bound():
    source = ("uniform int steps;\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  for (int i = 0; i < steps; i++) { fragColor = vec4(0.5); }\n}")
    findings = CHECKS.lint(source)
    assert [f.rule for f in findings] == ["for-bound"]
    assert findings[0].line == 3


def test_line_numbers_survive_block_comments():
    source = ("/* header\n   comment */\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
              "  while (1) { }\n}")
    findings = CHECKS.lint(source)
    assert [(f.rule, f.line) for f in findings] == [("while-true", 4)]
