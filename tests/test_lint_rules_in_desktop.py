"""The Desktop half embeds `lint_rules.json` as a constant; this fails when the copies drift.

A rule edited in `lint_rules.json` and not in `desktop/plugin.js` (or the reverse) would let a
shader that the server lints clean fail locally, or the other way round. Comparing the serialised
strings catches a changed pattern, severity, message or hint without running JavaScript.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_JS = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
RULES = json.loads((ROOT / "lint_rules.json").read_text(encoding="utf-8"))["rules"]


def js_escaped(text: str) -> str:
    """The text as it appears inside a single-quoted JS string (backslashes doubled)."""
    return json.dumps(text)[1:-1]


def test_every_rule_is_embedded_verbatim():
    for rule in RULES:
        assert f"id: '{rule['id']}'" in PLUGIN_JS, f"{rule['id']} missing from desktop/plugin.js"
        assert f"severity: '{rule['severity']}'" in PLUGIN_JS
        if rule.get("pattern"):
            assert js_escaped(rule["pattern"]) in PLUGIN_JS, f"pattern of {rule['id']} differs"
        assert js_escaped(rule.get("message", "")) in PLUGIN_JS, f"message of {rule['id']} differs"
        assert js_escaped(rule.get("hint", "")) in PLUGIN_JS, f"hint of {rule['id']} differs"


def test_no_extra_rule_lives_only_in_the_desktop_half():
    embedded = PLUGIN_JS.count("kind: '")
    assert embedded == len(RULES), (embedded, len(RULES))


def test_the_reserved_list_and_cap_are_documented_once():
    assert "const MAX_SLIDERS = 16" in PLUGIN_JS
    assert "iResolution" in PLUGIN_JS and "fragColor" in PLUGIN_JS
