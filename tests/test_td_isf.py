"""TouchDesigner GLSL TOP and ISF exports: file shape without a browser, shim proofs with one.

The `chromium` tests render each file in its host shim (`headless/harness.html`) next to the
source; `scripts/check.sh` skips them on hosts without a browser.
"""

import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import exporting as EXPORTING  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import header as HEADER  # noqa: E402
from shaderbruecke.exporters import isf as ISF  # noqa: E402
from shaderbruecke.exporters import registry as REGISTRY  # noqa: E402
from shaderbruecke.exporters import td as TD  # noqa: E402

FIXTURE_NAMES = [("sky.glsl", "golden-hour sky"), ("raymarch.glsl", "ice block"),
                 ("sliders.glsl", "striped plasma")]
# The td-shim's binding pattern (headless/harness.html tdBindings), kept in step by the test below.
BINDING = re.compile(r"^uniform\s+(?:float|vec[234])\s+(\w+)\s*;\s*// Vectors page: (\w+) = ([^(\n]*)",
                     re.M)


def doc(fixture="sky.glsl", name="golden-hour sky", **extra):
    source = (FIXTURES / fixture).read_text(encoding="utf-8")
    out = {"id": "20261109-000000-000000-test", "version": 1, "name": name, "source": source,
           "sliders": RECORD.parse_sliders(source), "channels": [],
           "provenance": {"origin": "agent", "url": None, "author": None, "licence": "CC-BY-4.0",
                          "parent": None},
           "checks": {"status": "ok"}, "edited_by": "agent", "created_at": "2026-11-09T00:00:00Z",
           "files": {}}
    out.update(extra)
    return out


def td_text(document, credit=True):
    return TD.files(document, {"credit": credit})[f"{HEADER.pascal(document['name'])}.td.glsl"]


def isf_text(document, credit=True):
    return ISF.files(document, {"credit": credit})[f"{HEADER.pascal(document['name'])}.fs"]


def isf_json(text):
    """The JSON the way hosts read it: between the first `/*` and the first `*/`."""
    assert text.startswith("/*{")
    return json.loads(text[2:text.index("*/")])


# Registry and file names ---------------------------------------------------------------------------

def test_registry_rows():
    td, isf = REGISTRY.EXPORT_TARGETS["td"], REGISTRY.EXPORT_TARGETS["isf"]
    assert (td.proof, td.status, td.main_file) == ("td-shim", "experimental", "{name}.td.glsl")
    assert (isf.proof, isf.status, isf.main_file) == ("isf-shim", "experimental", "{name}.fs")
    assert "TouchDesigner" in td.label and "ISF" in isf.label
    assert list(td.files(doc(), {})) == ["GoldenHourSky.td.glsl"]
    assert list(isf.files(doc(), {})) == ["GoldenHourSky.fs"]


def test_steps_say_experimental_and_why():
    for module in (TD, ISF):
        steps = module.steps(doc(), None)
        assert steps[0].startswith("Experimental") and "shim" in steps[0] and "not yet" in steps[0]


# TouchDesigner ------------------------------------------------------------------------------------

def test_td_pixel_shader_shape():
    text = td_text(doc())
    assert "layout(location = 0) out vec4 fragColor;" in text
    assert "mainImage(color, vUV.st * uTDOutputInfo.res.zw);" in text
    assert "fragColor = TDOutputSwizzle(color);" in text
    assert "#define iResolution vec3(uTDOutputInfo.res.zw, 1.0)" in text
    assert "#version" not in text.replace("adds #version", "")
    assert "uniform vec3 iResolution" not in text
    assert "sTD2DInputs" not in text                        # no channels, no inputs
    assert doc()["source"].strip() in text


def test_td_every_declared_uniform_has_a_vectors_row_with_the_effective_value():
    document = doc(tuned_values={"sunHeight": 0.41})
    text = td_text(document)
    declared = re.findall(r"^uniform\s+\w+\s+(\w+)\s*;", text, re.M)
    rows = {m.group(1): m.group(3).split() for m in BINDING.finditer(text) if m.group(1) == m.group(2)}
    assert set(declared) == set(rows) == {"iTime", "sunHeight", "cloudCover", "warmth"}
    assert rows["iTime"] == ["absTime.seconds"]
    effective = RECORD.effective_values(document)
    assert {name: float(rows[name][0]) for name in effective} == effective
    assert "range 0.05..0.95, step 0.01; tuned, authored 0.32" in text


def test_td_steps_name_every_vectors_row():
    document = doc("sliders.glsl", "striped plasma")
    steps = "\n".join(TD.steps(document, None))
    for row in TD.bindings(document):
        assert f"`{row['name']}`" in steps and f"`{row['value']}`" in steps
    assert "absTime.seconds" in steps and "Custom Resolution" in steps and "info DAT" in steps


def test_td_frame_counter_arrives_as_float_and_is_read_as_int():
    document = doc(source="void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                          "  fragColor = vec4(float(iFrame % 2), iTimeDelta, iMouse.x, 1.0);\n}",
                   sliders=[])
    text = td_text(document)
    assert "uniform float sbFrame;  // Vectors page: sbFrame = absTime.frame" in text
    assert "#define iFrame int(sbFrame)" in text
    assert "uniform float iTimeDelta;  // Vectors page: iTimeDelta = absTime.stepSeconds" in text
    assert "uniform vec4 iMouse;  // Vectors page: iMouse = 0 0 0 0" in text
    assert "uniform float iTime;" not in text               # unused: no row to add


def test_td_channels_only_when_the_record_has_them():
    assert "sTD2DInputs" not in td_text(doc())
    text = td_text(doc(channels=[{"kind": "image"}, {"kind": "image"}]))
    assert "#define iChannel0 sTD2DInputs[0]" in text and "#define iChannel1 sTD2DInputs[1]" in text
    assert "bottom left" in text
    steps = "\n".join(TD.steps(doc(channels=[{"kind": "image"}]), None))
    assert "iChannel0" in steps and "flip" in steps


# ISF ----------------------------------------------------------------------------------------------

def test_isf_json_is_first_and_parses():
    meta = isf_json(isf_text(doc()))
    assert meta["ISFVSN"] == "2.0"
    assert meta["CATEGORIES"] == ["Generator"]
    assert "golden-hour sky" in meta["DESCRIPTION"] and "@1" in meta["DESCRIPTION"]
    assert "Licence: CC-BY-4.0" in meta["CREDIT"] and HEADER.CREDIT in meta["CREDIT"]


def test_isf_inputs_are_the_sliders_at_their_effective_values():
    document = doc(tuned_values={"sunHeight": 0.41})
    inputs = isf_json(isf_text(document))["INPUTS"]
    assert [i["NAME"] for i in inputs] == ["sunHeight", "cloudCover", "warmth"]
    assert all(i["TYPE"] == "float" for i in inputs)
    assert {i["NAME"]: i["DEFAULT"] for i in inputs} == RECORD.effective_values(document)
    first = inputs[0]
    assert (first["MIN"], first["MAX"], first["LABEL"]) == (0.05, 0.95, "sunHeight")


def test_isf_code_uses_the_host_names_and_declares_none():
    text = isf_text(doc())
    code = text[text.index("*/") + 2:]
    assert "#define iResolution vec3(RENDERSIZE, 1.0)" in code and "#define iTime TIME" in code
    assert "mainImage(color, isf_FragNormCoord * RENDERSIZE);" in code
    assert "gl_FragColor = color;" in code
    assert not re.search(r"^\s*uniform\b", code, re.M)
    assert "#version" not in code and "precision" not in code


def test_isf_has_no_audio_and_images_only_with_channels():
    for fixture, name in FIXTURE_NAMES:
        text = isf_text(doc(fixture, name))
        assert "audio" not in text.lower()
        assert "IMG_NORM_PIXEL" not in text
        assert all(i["TYPE"] == "float" for i in isf_json(text)["INPUTS"])
    with_channel = doc(channels=[{"kind": "image"}],
                       source="void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
                              "  fragColor = texture(iChannel0, fragCoord / iResolution.xy);\n}",
                       sliders=[])
    text = isf_text(with_channel)
    assert isf_json(text)["INPUTS"] == [{"NAME": "iChannel0", "TYPE": "image", "LABEL": "iChannel0"}]
    assert "IMG_NORM_PIXEL(iChannel0, fragCoord / iResolution.xy)" in text
    assert "audioFFT" in "\n".join(ISF.steps(doc(), None))   # says audio is not exported yet


def test_isf_hostile_metadata_cannot_close_the_json_comment():
    document = doc(name='sky */ void main() {} /* "x"\n', tuned_values={})
    document["provenance"].update(author="eve */ evil", url="https://x.test/*/")
    document["sliders"][0]["label"] = 'Sun */ height "quoted"'
    text = isf_text(document)
    meta = isf_json(text)                                   # parses, so the first */ is ours
    assert text.index("*/") == text.index("}*/") + 1
    assert meta["INPUTS"][0]["LABEL"] == 'Sun * / height "quoted"'


def test_isf_credit_line_follows_the_setting():
    assert HEADER.CREDIT not in isf_text(doc(), credit=False)
    assert HEADER.CREDIT in isf_text(doc(), credit=True)


def test_snippets_name_the_hosts():
    assert "GLSL TOP" in EXPORTING._snippet("td", "Sky")
    assert "Sky.fs" in EXPORTING._snippet("isf", "Sky")


# Shim proofs --------------------------------------------------------------------------------------

@pytest.mark.chromium
@pytest.mark.parametrize("target", ["td", "isf"])
@pytest.mark.parametrize("fixture,name", FIXTURE_NAMES)
def test_fixture_matches_in_the_host_shim(fixture, name, target, tmp_path):
    document = doc(fixture, name)
    row = REGISTRY.EXPORT_TARGETS[target]
    proof = EXPORTING.prove(document, row, row.files(document, {"credit": True}), tmp_path)
    assert proof["method"] == row.proof
    assert proof["verdict"] == "match", proof
    expected = 6 if document["sliders"] else 3
    assert len(proof["checks"]) == expected
    assert proof["diff"]["mean"] <= EXPORTING.PROOF_MEAN_LIMIT
    assert proof["diff"]["p99"] <= EXPORTING.PROOF_P99_LIMIT
    assert "has not rendered it yet" in proof["why"]
    assert len(proof["frames"]) == 2


def mutated(target, document, old, new):
    row = REGISTRY.EXPORT_TARGETS[target]
    files = row.files(document, {"credit": True})
    key = next(iter(files))
    assert old in files[key], old
    files[key] = files[key].replace(old, new, 1)
    return row, files


@pytest.mark.chromium
@pytest.mark.parametrize("target,old,new", [
    # the Vectors value the steps give is the authored default, not the tuned value
    ("td", "Vectors page: frequency = 7", "Vectors page: frequency = 5"),
    # time bound to the frame counter instead of seconds
    ("td", "Vectors page: iTime = absTime.seconds", "Vectors page: iTime = absTime.frame"),
    # the ISF host would show the authored default, not the tuned one
    ('isf', '"DEFAULT": 7.0', '"DEFAULT": 5.0'),
    ("isf", "#define iTime TIME", "#define iTime (TIME + 0.5)"),
])
def test_a_drifting_host_file_is_reported_as_differs(target, old, new, tmp_path):
    # (the sky's sunHeight is too gentle for this: 0.41 against 0.32 stays within 0.5/255 mean)
    document = doc("sliders.glsl", "striped plasma", tuned_values={"frequency": 7.0})
    row, files = mutated(target, document, old, new)
    proof = EXPORTING.prove(document, row, files, tmp_path)
    assert proof["verdict"] == "differs", proof


@pytest.mark.chromium
@pytest.mark.parametrize("target,old,new,reason", [
    ("td", "// golden-hour sky", "#version 460\n// golden-hour sky", "#version"),
    ("td", "uniform float warmth;", "float warmthUnbound;", "compile"),
    ("isf", "/*{", "// note\n/*{", "first thing"),
    ("isf", '"ISFVSN": "2.0",', '"ISFVSN": "2.0"', "JSON"),
    ("isf", "#define iTime TIME", "uniform float TIME;\n#define iTime TIME", "compile"),
])
def test_a_broken_host_file_is_not_proven(target, old, new, reason, tmp_path):
    row, files = mutated(target, doc(), old, new)
    proof = EXPORTING.prove(doc(), row, files, tmp_path)
    assert proof["verdict"] == "not_proven", proof
    assert reason in proof["why"], proof["why"]


def test_channels_are_not_proven_without_a_browser(tmp_path):
    document = doc(channels=[{"kind": "image"}])
    for target in ("td", "isf"):
        row = REGISTRY.EXPORT_TARGETS[target]
        proof = EXPORTING.prove(document, row, row.files(document, {}), tmp_path)
        assert proof["verdict"] == "not_proven" and "channel" in proof["why"]
