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
    assert REGISTRY.target_ids() == ["three", "tsl", "r3f", "agent", "shadertoy"]
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
    assert "const float frequency = 5;" in text
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
