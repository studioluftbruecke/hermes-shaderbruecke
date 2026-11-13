"""The lint rules belong to the code that loaded them.

A running gateway keeps the plugin's Python modules until a reload, but `checks.lint` read
`lint_rules.json` from disk on every call. When the files on disk moved ahead (a checkout update,
a `hermes plugins update`, a re-pointed link) the old code met a rule kind it had no handler for,
and every render failed with `unknown lint rule kind: slider_decl`. The rules are now read once,
when the module loads, so code and rules always come from the same version.
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }"


def fresh_checks():
    spec = importlib.util.spec_from_file_location("shaderbruecke_checks_load_once", ROOT / "checks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_newer_rules_file_on_disk_does_not_reach_loaded_code(tmp_path):
    checks = fresh_checks()
    newer = tmp_path / "lint_rules.json"
    newer.write_text(json.dumps({"version": 9, "rules": [
        {"id": "from-the-future", "kind": "kind_this_code_does_not_know", "severity": "error"}]}))
    checks.RULES_PATH = newer
    assert checks.lint(SOURCE) == []


def test_the_loaded_rules_are_the_shipped_table():
    checks = fresh_checks()
    shipped = json.loads((ROOT / "lint_rules.json").read_text(encoding="utf-8"))["rules"]
    assert [rule["id"] for rule in checks.RULES] == [rule["id"] for rule in shipped]
