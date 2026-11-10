"""The Desktop half and the Python half agree on `@slider` parsing.

The test reads the regex and the reserved-name list out of `desktop/plugin.js` and runs the same
pipeline as `record.parse_sliders` in Python. A change to one side without the other fails here,
so a slider shader that renders on the server cannot silently break in Desktop.

Syntax: `// @slider <name> <min>..<max> = <default> [step <s>] ["Human label"]`. Both sides give
every slider a `label` (a string, or None without one); a side that leaves the key out is read as
None, so the comparison holds the label values, not the dict layout.
"""

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_JS = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def load_record():
    spec = importlib.util.spec_from_file_location("shaderbruecke_record", ROOT / "record.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECORD = load_record()


def js_regex() -> re.Pattern:
    match = re.search(r"^const SLIDER_RE = /(.+)/$", PLUGIN_JS, re.MULTILINE)
    assert match, "SLIDER_RE not found in desktop/plugin.js"
    return re.compile(match.group(1))


def js_reserved() -> set[str]:
    block = PLUGIN_JS.split("const SLIDER_RESERVED = new Set((", 1)[1].split(".split(' '))", 1)[0]
    words = " ".join(re.findall(r"'([^']*)'", block)).split()
    return set(words)


def js_parse(source: str) -> list[dict]:
    """The Desktop algorithm, in Python: same regex, same reserved list, same caps."""
    pattern, reserved = js_regex(), js_reserved()
    sliders, seen = [], set()
    for line in source.split("\n"):
        match = pattern.match(line)
        if not match:
            continue
        name = match.group(1)
        low, high, default = float(match.group(2)), float(match.group(3)), float(match.group(4))
        if name in reserved or name in seen or len(sliders) >= 16:
            continue
        if not (high > low) or default < low or default > high:
            continue
        label = match.group(6).strip() if match.group(6) and match.group(6).strip() else None
        sliders.append({"name": name, "min": low, "max": high, "default": default,
                        "step": float(match.group(5)) if match.group(5) else None, "label": label})
        seen.add(name)
    return sliders


SAMPLES = [
    "// @slider gain 0.0..2.0 = 1.0\n",
    "// @slider sunHeight 0.05..0.95 = 0.32 step 0.01\n",
    "// @slider negative -1.0..1.0 = -0.25 step 0.05\n",
    "  // @slider indented 0..10 = 4\n",
    "// @slider 2fast 0.0..1.0 = 0.5\n",
    "// @slider vec3 0.0..1.0 = 0.5\n",
    "// @slider iResolution 0.0..1.0 = 0.5\n",
    "// @slider backwards 1.0..0.0 = 0.5\n",
    "// @slider outside 0.0..1.0 = 2.0\n",
    "// @slider noDefault 0.0..1.0\n",
    "//@slider tight 0.0..1.0=0.5\n",
    "// text mentioning @slider later 0.0..1.0 = 0.5\n",
    "@slider notacomment 0.0..1.0 = 0.5\n",
    "".join(f"// @slider s{i} 0.0..1.0 = 0.5\n" for i in range(20)),
    '// @slider sunHeight 0.05..0.95 = 0.32 step 0.01 "Sun height above the horizon"\n',
    '// @slider glow 0.0..1.0 = 0.5 "Glow"\n',
    '// @slider spaced 0.0..1.0 = 0.5   "  Padded label  "  \n',
    '// @slider blank 0.0..1.0 = 0.5 ""\n',
    '// @slider unclosed 0.0..1.0 = 0.5 "no end\n',
    '// @slider bare 0.0..1.0 = 0.5 Label without quotes\n',
    '// @slider stepAfter 0.0..1.0 = 0.5 "Label first" step 0.1\n',
]


def normalised(sliders: list[dict]) -> list[dict]:
    return [{**slider, "label": slider.get("label")} for slider in sliders]


def test_reserved_lists_match():
    assert js_reserved() == set(RECORD.RESERVED), (
        js_reserved() ^ set(RECORD.RESERVED))


def test_both_parsers_agree_on_every_sample():
    for sample in SAMPLES:
        assert normalised(js_parse(sample)) == normalised(RECORD.parse_sliders(sample)), sample


def test_the_desktop_side_reads_labels():
    # Desktop only: holds the JS regex to the syntax even before the server side reads labels.
    assert js_parse(SAMPLES[14])[0]["label"] == "Sun height above the horizon"
    assert js_parse(SAMPLES[16])[0]["label"] == "Padded label"
    assert js_parse(SAMPLES[17])[0]["label"] is None  # an empty label falls back to the name
    assert js_parse(SAMPLES[18]) == [] and js_parse(SAMPLES[19]) == [] and js_parse(SAMPLES[20]) == []
    assert js_parse(SAMPLES[0])[0]["label"] is None


def test_both_parsers_agree_on_the_sky_fixture():
    fixture = (ROOT / "tests" / "fixtures" / "sky.glsl").read_text(encoding="utf-8")
    parsed = RECORD.parse_sliders(fixture)
    assert [s["name"] for s in parsed] == ["sunHeight", "cloudCover", "warmth"]
    assert normalised(js_parse(fixture)) == normalised(parsed)
