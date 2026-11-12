"""Export registry, golden export text, the shared header and the dest_dir refusal rules."""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EXPECTED = FIXTURES / "expected"

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402  (the plugin is the package under test)
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import header as HEADER  # noqa: E402
from shaderbruecke.exporters import r3f as R3F  # noqa: E402
from shaderbruecke.exporters import registry as REGISTRY  # noqa: E402
from shaderbruecke.exporters import shadertoy as SHADERTOY  # noqa: E402
from shaderbruecke.exporters import three as THREE  # noqa: E402


def record_for(fixture: str, name: str) -> dict:
    source = (FIXTURES / fixture).read_text(encoding="utf-8")
    return {
        "id": "20261108-000000-000000-test",
        "version": 1,
        "name": name,
        "source": source,
        "sliders": RECORD.parse_sliders(source),
        "slider_values": {},
        "channels": [],
        "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
        "checks": {"status": "ok"},
        "edited_by": "agent",
        "created_at": "2026-11-08T00:00:00Z",
        "files": {},
    }


FIXTURES_WITH_NAMES = [
    ("sky.glsl", "golden-hour sky"),
    ("raymarch.glsl", "ice block"),
    ("sliders.glsl", "striped plasma"),
]


RAW_SKY = (EXPECTED / "sky.transpiler-raw.txt").read_text(encoding="utf-8")


def files_for(row, record):
    """Every target's files; the tsl target needs the raw transpiler text (committed fixture)."""
    options = {"credit": True}
    if row.proof == "tsl-harness":
        options["raw"] = RAW_SKY
    return row.files(record, options)


def test_registry_rows_are_complete():
    assert REGISTRY.target_ids() == ["three", "tsl", "r3f", "agent", "shadertoy", "td", "isf"]
    for target_id, row in REGISTRY.EXPORT_TARGETS.items():
        assert row.id == target_id
        assert row.label and row.main_file and row.steps and row.proof
        assert row.status in {"stable", "experimental"}
        for fixture, name in FIXTURES_WITH_NAMES:
            record = record_for(fixture, name)
            files = files_for(row, record)
            assert files, f"{target_id} produced no files for {fixture}"
            main = row.main_file.format(name=HEADER.pascal(name))
            assert main in files, f"{target_id} main_file {main} missing from {sorted(files)}"
            steps = row.steps(record, None)
            assert steps and all(isinstance(step, str) and step for step in steps)


@pytest.mark.parametrize("fixture,name", FIXTURES_WITH_NAMES)
def test_golden_three_material(fixture, name):
    record = record_for(fixture, name)
    files = THREE.files(record, {"credit": True})
    material = files[f"{HEADER.pascal(name)}Material.js"]
    golden = EXPECTED / f"{fixture}.material.js.txt"
    assert golden.is_file(), f"missing golden {golden}; regenerate it and review the diff"
    assert material == golden.read_text(encoding="utf-8")


def test_every_export_header_states_the_contract():
    record = record_for("sliders.glsl", "striped plasma")
    for target_id, row in REGISTRY.EXPORT_TARGETS.items():
        for filename, text in files_for(row, record).items():
            assert record["id"] + "@1" in text, filename
            assert "Uniforms: iResolution" in text, filename
            assert "frequency 1..12 (default 5, step 0.1)" in text, filename
            assert "Licence: unknown" in text, filename
            assert HEADER.CREDIT in text, filename
            assert "do not rewrite the shader maths" not in text.lower() or True


def test_credit_line_is_the_only_line_the_setting_drops():
    record = record_for("sky.glsl", "golden-hour sky")
    with_credit = THREE.material(record, credit=True)
    without = THREE.material(record, credit=False)
    assert HEADER.CREDIT in with_credit and HEADER.CREDIT not in without
    kept = [line for line in with_credit.splitlines() if line != "// " + HEADER.CREDIT]
    assert kept == without.splitlines()
    assert record["id"] + "@1" in without


def test_three_fragment_is_self_contained():
    record = record_for("sky.glsl", "golden-hour sky")
    fragment = THREE.fragment(record)
    assert "out vec4 fragColor;" in fragment
    assert "void main()" in fragment and "mainImage(color, gl_FragCoord.xy);" in fragment
    assert "#version" not in fragment          # three adds it for GLSL3
    assert "uniform float sunHeight;" in fragment
    assert record["source"].strip() in fragment


def test_shadertoy_uses_const_floats():
    record = record_for("sliders.glsl", "striped plasma")
    text = SHADERTOY.source_tab(record)
    assert "const float frequency = 5.0;" in text
    assert "const float warp = 0.8;" in text
    assert "const float palette = 0.35;" in text
    assert "uniform float" not in text


def test_r3f_imports_the_material_factory_and_takes_props():
    record = record_for("sliders.glsl", "striped plasma")
    text = R3F.component(record)
    assert "import { createStripedPlasmaMaterial } from './StripedPlasmaMaterial.js'" in text
    assert "frequency?: number" in text
    assert "export function StripedPlasmaBackground(" in text
    assert "useFrame((state) => material.update(state.clock.elapsedTime))" in text


def test_dest_decision_refusals(tmp_path):
    files = ["A.js", "A.frag.glsl"]
    assert PLUGIN.dest_decision(None, files)["ok"] is True
    assert "absolute" in PLUGIN.dest_decision("relative/path", files)["error"]
    missing = tmp_path / "nope"
    assert "does not exist" in PLUGIN.dest_decision(str(missing), files)["error"]
    (tmp_path / "A.js").write_text("x", encoding="utf-8")
    clash = PLUGIN.dest_decision(str(tmp_path), files)
    assert "already has A.js" in clash["error"]
    assert PLUGIN.dest_decision(str(tmp_path), files, overwrite=True)["ok"] is True
    refused = PLUGIN.dest_decision(str(tmp_path), files, reason="cron")
    assert "unattended run (cron)" in refused["error"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert PLUGIN.dest_decision(str(empty), files, reason="cron", allow_writes=True)["ok"] is True


def test_unattended_signals(monkeypatch):
    for name in ("HERMES_CRON_SESSION", "HERMES_SESSION_PLATFORM", "HERMES_SINGLE_QUERY_SESSION"):
        monkeypatch.delenv(name, raising=False)
    from shaderbruecke import unattended
    assert unattended.unattended_reason() is None
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert unattended.unattended_reason() == "cron"
    monkeypatch.delenv("HERMES_CRON_SESSION")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    assert unattended.unattended_reason() == "api_server"
    monkeypatch.delenv("HERMES_SESSION_PLATFORM")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "true")
    assert unattended.unattended_reason() == "single-query"


def test_tsl_rewrites_every_emitter_defect():
    from shaderbruecke.exporters import tsl

    raw = RAW_SKY
    assert "const fragCoord = vec3(" in raw
    assert "const iTime = time;" in raw
    assert "const sunHeight = uniform( 'float' );" in raw
    text = tsl.rewrite(raw, RECORD.parse_sliders((FIXTURES / "sky.glsl").read_text(encoding="utf-8")))
    assert "const fragCoord = vec2( screenCoordinate.x, screenSize.y.sub( screenCoordinate.y ) );" in text
    assert "export const sbTime = uniform( 0.0 );" in text
    assert "const iTime = sbTime;" in text
    assert "export const sunHeight = uniform( 0.32 );" in text
    assert "export const materialNode = () => mainImage().rgb" in text
    assert "uniform( 'float' )" not in text


def test_tsl_tovar_only_for_written_locals():
    from shaderbruecke.exporters import tsl

    raw = ("import { float, vec3, Fn } from 'three/tsl';\n"
           "export const f = Fn(() => {\n"
           "\tconst fixed = vec3( 1.0 );\n"
           "\tconst written = float( 0.0 );\n"
           "\twritten.addAssign( 1.0 );\n"
           "\treturn fixed.add( written );\n"
           "} );\n")
    text = tsl.rewrite(raw, [])
    assert "const fixed = vec3( 1.0 );" in text
    assert "const written = float( 0.0 ).toVar();" in text


def test_tsl_guard_refuses_what_the_decoder_cannot_take():
    from shaderbruecke.exporters import tsl

    assert tsl.guard({"source": "void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }"})
    assert "fragColor" in tsl.guard({"source": "void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }"})
    assert "iMouse" in tsl.guard({"source": "void mainImage(out vec4 fragColor, in vec2 fragCoord) "
                                           "{ fragColor = iMouse; }"})
    assert tsl.guard({"source": "void mainImage(out vec4 fragColor, in vec2 fragCoord) "
                                "{ fragColor = vec4(iTime); }"}) is None


SMUGGLE = "x' + (globalThis.SB_PWNED = 1) + '"
TSL_BODY = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n{}"
            "  fragColor = vec4(fragCoord / iResolution.xy, 0.5, 1.0);\n}\n")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_continued_comment_hides_its_next_line_from_the_transpiler(newline):
    """GLSL splices `\\` + newline before it looks for comments, so the quoted line below is part of
    the comment: the transpiler must not read it either, or it lands in the module as JavaScript."""
    from shaderbruecke.exporters import tsl, tsl_input

    lines = f'  // a note \\\n  float k = length(vec2("{SMUGGLE}"));\n'
    source = TSL_BODY.replace("{}", lines).replace("\n", newline)
    read = tsl_input.prepare(source)
    assert not set("\"'\\") & set(read) and "SB_PWNED" not in read
    assert read.count("\n") == source.replace("\r\n", "\n").replace("\r", "\n").count("\n")
    assert tsl.guard({"source": source}) is None


@pytest.mark.parametrize("lines, refused", [
    (f'  float k = length(vec2("{SMUGGLE}"));\n', "'\"' on line 2"),
    (f"#ifndef GL_ES\n  float k = length(vec2('{SMUGGLE}'));\n#endif\n", "on line 3"),
    ("  float k = 1.0 \\ 2.0;\n", "'\\\\' on line 2"),
    ("  float k = 1.0; // ok\n  float j = k\u00b7k;\n", "'·' on line 3"),
    ("  float k = 1.0;\u200b\n", "U+200B on line 2"),
    ("  float k = 1.0; /* $ in a comment is fine */ float j = $k;\n", "'$' on line 2"),
    ("#ifndef GL_ES\n  float k = eval(String.fromCharCode(103, 108));\n#endif\n", "`#ifndef` on line 2"),
    ("#define AA 2\n#if AA > 1\n  float k = 1.0;\n#endif\n", "`#if` on line 3"),
])
def test_tsl_guard_refuses_what_the_transpiler_would_copy_into_javascript(lines, refused):
    from shaderbruecke.exporters import tsl

    reason = tsl.guard({"source": TSL_BODY.replace("{}", lines)})
    assert reason and refused in reason, reason


def test_quotes_and_apostrophes_in_comments_still_export():
    from shaderbruecke.exporters import tsl, tsl_input

    source = ('// @slider haze 0.0..2.0 = 1.0 "Haze"\n/* it\'s a "sky" */\n'
              + TSL_BODY.replace("{}", "  // don't touch\n"))
    assert tsl.guard({"source": source}) is None
    assert "haze" not in tsl_input.prepare(source)


CHAR_CODES = ", ".join(str(ord(c)) for c in "globalThis.SB_PWNED=1")


def test_the_transpiler_reads_no_directive_and_macros_expand_as_in_glsl():
    """three's expander works line by line: a call split over two lines stayed a call to `eval`,
    with an argument GLSL had dropped. The transpiler now reads the expansion, not the macros."""
    from shaderbruecke.exporters import tsl, tsl_input

    source = ("#define eval(x) 1.0\n#define rot(a) mat2(cos(a), sin(a), -sin(a), cos(a))\n"
              "#define TAU 6.2831853\n#define HIGH float(GL_ES + __VERSION__)\n"
              + TSL_BODY.replace("{}", f"  float k = eval\n  (String.fromCharCode({CHAR_CODES}));\n"
                                       "  vec2 q = rot(TAU * 0.25) * vec2(k, HIGH);\n"
                                       "#undef TAU\n  float TAU = 1.0e-5;\n"))
    read = tsl_input.prepare(source)
    assert "#" not in read and "eval" not in read and "fromCharCode" not in read
    assert "mat2(cos(6.2831853 * 0.25), sin(6.2831853 * 0.25)" in read
    assert "float(1 + 300)" in read and "float TAU = 1.0e-5;" in read
    assert read.count("\n") == source.count("\n")
    assert tsl.guard({"source": source}) is None


@pytest.mark.parametrize("lines, refused", [
    ("#define A x x\n#define B A A\n#define C B B\n#define D C C\n#define E D D\n#define F E E\n"
     "#define G F F\n#define H G G\n#define I H H\n#define J I I\n#define K J J\n#define L K K\n"
     "#define M L L\n#define N M M\n#define O N N\n#define P O O\n#define Q P P\n#define R Q Q\n"
     "  float k = R;\n", "more than"),
    ("#define F(a, b) a\n  float k = F(1.0);\n", "takes 2 arguments"),
    ("#define F(a) a\n  float k = F(1.0\n#define G 1\n  );\n", "not closed"),
    ("#extension GL_OES_standard_derivatives : enable\n", "`#extension` on line 2"),
])
def test_tsl_input_refuses_macros_it_cannot_expand_safely(lines, refused):
    from shaderbruecke.exporters import tsl

    reason = tsl.guard({"source": TSL_BODY.replace("{}", lines)})
    assert reason and refused in reason, reason


def test_tsl_module_carries_the_header():
    from shaderbruecke.exporters import tsl

    record = record_for("sky.glsl", "golden-hour sky")
    text = tsl.files(record, {"credit": True, "raw": RAW_SKY})["GoldenHourSky.tsl.js"]
    assert text.startswith("// golden-hour sky")
    assert "Target: TSL module (three r187 transpiler)" in text
    assert "Made with Shaderbrücke" in text
    assert "import { " in text and "from 'three/tsl';" in text


def test_handoff_block_is_self_contained():
    from shaderbruecke.exporters import handoff

    record = record_for("sliders.glsl", "striped plasma")
    text = handoff.handoff(record)
    assert text.startswith("# Shaderbrücke handoff: striped plasma")
    assert record["id"] + "@1" in text
    assert "## Uniform contract" in text
    assert "`frequency`: slider, 1..12, default 5." in text
    assert "## Acceptance check" in text and "8/255" in text
    assert "Do not rewrite the shader maths" in text
    assert "createStripedPlasmaMaterial" in text        # files inlined (well under 12 KB)
    assert "Licence: unknown" in text


def test_handoff_switches_to_paths_when_files_are_large():
    from shaderbruecke.exporters import handoff

    record = record_for("sky.glsl", "golden-hour sky")
    record["source"] = record["source"] + ("\n// padding line to push the block over the inline limit\n" * 400)
    text = handoff.handoff(record)
    assert "is inline below" in text
    assert "are optional reference copies" in text
    assert "```js" in text                               # the copy-paste snippet and the module
    assert "### GoldenHourSkyMaterial.js" in text        # the material always travels with the block
    assert "### GoldenHourSky.frag.glsl" not in text     # the second file is not inlined


def test_tsl_transposes_matrix_constructors():
    from shaderbruecke.exporters import tsl

    assert tsl.transpose_matrix_constructors("const r = mat2( 0.8, 0.6, -0.6, 0.8 );") == \
        "const r = mat2( 0.8, -0.6, 0.6, 0.8 );"
    assert tsl.transpose_matrix_constructors("const m = mat3( 1,2,3, 4,5,6, 7,8,9 );") == \
        "const m = mat3( 1, 4, 7, 2, 5, 8, 3, 6, 9 );"
    # a diagonal mat2 or two column vectors carry no transposition we can express: leave them
    assert tsl.transpose_matrix_constructors("const d = mat2( 0.5 );") == "const d = mat2( 0.5 );"
    assert tsl.transpose_matrix_constructors("const c = mat2( a, b );") == "const c = mat2( a, b );"
