"""Correctness and safety at the boundary: a fresh profile, ids that become paths, hostile text in
generated files, copy-after-proof, fork paths and the plugin folder staying clean.
"""

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
RAW_SKY = (FIXTURES / "expected" / "sky.transpiler-raw.txt").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.exporters import header as HEADER  # noqa: E402
from shaderbruecke.exporters import registry as REGISTRY  # noqa: E402
from shaderbruecke.exporters import three as THREE  # noqa: E402

LINT_FAIL = "void main() {}"
OK_SOURCE = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """A fresh profile: an empty database file and an empty data directory."""
    db = tmp_path / "fresh.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    return db, base


def document(shader_id="sky", name="golden-hour sky", source=None, base=None):
    source = source or (FIXTURES / "sky.glsl").read_text(encoding="utf-8")
    return {"id": shader_id, "version": 1, "name": name, "source": source,
            "sliders": RECORD.parse_sliders(source), "channels": [],
            "provenance": {"origin": "agent", "url": None, "author": None, "licence": None,
                           "parent": None},
            "checks": {"status": "ok"}, "edited_by": "agent",
            "created_at": "2026-11-08T00:00:00Z", "files": {}}


def call(handler, args):
    return json.loads(handler(args))


# A fresh profile --------------------------------------------------------------------------------

def test_first_calls_on_a_fresh_profile_answer_instead_of_raising(storage):
    rendered = call(PLUGIN.shader_render, {"source": LINT_FAIL, "name": "first"})
    assert rendered["success"] is False and rendered["error"].startswith("lint_error")
    listed = call(PLUGIN.shader_library, {"action": "list"})
    assert listed == {"success": True, "count": 0, "total": 0, "cursor": None, "shaders": []}
    assert "no shader" in call(PLUGIN.shader_library, {"action": "get", "id": "sky"})["error"]
    assert "no shader" in call(PLUGIN.shader_export, {"id": "sky", "target": "three"})["error"]


# Ids that become paths --------------------------------------------------------------------------

BAD_IDS = ["../../escaped", "a/b", "..", "Upper", "-lead", "x" * 81, "a b", "a\nb"]


def test_valid_id_accepts_slugs_and_refuses_everything_else():
    assert RECORD.valid_id("sky")
    assert RECORD.valid_id(RECORD.new_id("golden-hour sky"))
    for bad in BAD_IDS + ["", None, 7]:
        assert not RECORD.valid_id(bad), bad


def test_a_traversal_id_is_refused_and_nothing_is_written(storage, tmp_path):
    for bad in BAD_IDS:
        for source in (LINT_FAIL, OK_SOURCE):
            result = call(PLUGIN.shader_render, {"source": source, "id": bad})
            assert result["success"] is False and "id" in result["error"], (bad, result)
        assert call(PLUGIN.shader_render, {"source": OK_SOURCE, "parent_id": bad})["success"] is False
        assert call(PLUGIN.shader_library, {"action": "get", "id": bad})["success"] is False
        assert call(PLUGIN.shader_library, {"action": "fork", "id": bad})["success"] is False
        assert call(PLUGIN.shader_export, {"id": bad, "target": "three"})["success"] is False
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.name != "fresh.db"]
    assert written == [], written
    with pytest.raises(ValueError):
        RECORD.version_dir(tmp_path, "../x", 1)


def test_an_unknown_id_is_an_error_that_names_parent_id(storage):
    result = call(PLUGIN.shader_render, {"source": OK_SOURCE, "id": "no-such-shader"})
    assert result["success"] is False
    assert "parent_id" in result["error"] and "omit id" in result["error"]


# Hostile text in generated files ----------------------------------------------------------------

HOSTILE = "sky\nglobalThis.PWNED='name'\r\n// */ ${globalThis.PWNED=1} `\u2028x\x00"


def hostile_record():
    record = document(name=HOSTILE)
    record["provenance"].update({"author": "eve\n*/ globalThis.PWNED='author'",
                                 "url": "https://x.test/\nglobalThis.PWNED='url'",
                                 "licence": "MIT\r\nglobalThis.PWNED='licence'"})
    return record


def every_file(record):
    out = {}
    for target_id, row in REGISTRY.EXPORT_TARGETS.items():
        options = {"credit": True}
        if row.proof == "tsl-harness":
            options["raw"] = RAW_SKY
        for filename, text in row.files(record, options).items():
            out[(target_id, filename.replace(HEADER.pascal(record["name"]), "N"))] = text
    return out


def test_hostile_names_never_leave_their_comment():
    hostile = every_file(hostile_record())
    benign = every_file(document(name="sky"))
    assert hostile.keys() == benign.keys()
    for key, text in hostile.items():
        assert len(text.splitlines()) == len(benign[key].splitlines()), key
        assert text.count("*/") == benign[key].count("*/"), key
        assert "\u2028" not in text and "\x00" not in text and "\r" not in text, key
        for line in text.splitlines():
            assert not line.lstrip().startswith("globalThis"), (key, line)


def test_header_text_strips_controls_neutralises_comment_closers_and_caps_length():
    assert HEADER.header_text("a\nb\r\tc*/d\x00e\u2028f") == "a b c* /d e f"
    assert len(HEADER.header_text("x" * 1000)) <= HEADER.HEADER_TEXT_LIMIT


def handoff_block(text, heading):
    """The body of the fenced block under `heading`, read the way CommonMark reads it."""
    lines = text.split("\n")
    start = lines.index(heading) + 1
    fence = lines[start]
    ticks = len(fence) - len(fence.lstrip("`"))
    assert ticks >= 3, fence
    body = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped and set(stripped) == {"`"} and len(stripped) >= ticks:
            return "\n".join(body)
        body.append(line)
    raise AssertionError("unterminated fence")


def test_handoff_fences_survive_backticks_in_the_source():
    from shaderbruecke.exporters import handoff

    source = "/* a fence in a comment:\n```\n````js\n*/\n" + OK_SOURCE + "\n"
    record = document(name="fenced", source=source)
    text = handoff.handoff(record)
    files = THREE.files(record, {"credit": True})
    assert handoff_block(text, "### FencedMaterial.js") == files["FencedMaterial.js"].rstrip("\n")
    assert handoff_block(text, "### Fenced.frag.glsl") == files["Fenced.frag.glsl"].rstrip("\n")


# Hostile comments in the source -----------------------------------------------------------------

HOSTILE_COMMENTS = [
    "// path C:\\new\\unit and \\u00e9 and \\x41\n",
    "// a note \\`;globalThis.PWNED='comment';const _rest=\\`\n",
    "// ${globalThis.PWNED='template'} and \\${also} and $${x}\n",
    "// backtick ` and */ and /* inside\n",
    "// trailing backslash \\\n",
    "// windows line\r\n// ending\r\n",
    "// separators \u2028 and \u2029 here\n",
    "/* block with \\\\` and ${1+1} */\n",
]


def node_stub(directory: pathlib.Path) -> None:
    three = directory / "node_modules" / "three"
    three.mkdir(parents=True, exist_ok=True)
    (three / "package.json").write_text(json.dumps({"name": "three", "type": "module",
                                                    "main": "index.js"}), encoding="utf-8")
    (three / "index.js").write_text(
        "export class Vector3 { set() {} }\nexport class Vector4 {}\n"
        "export class ShaderMaterial { constructor(o) { Object.assign(this, o) } }\n"
        "export const GLSL3 = '300 es'\n", encoding="utf-8")
    (directory / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to import the module")
def test_fragment_survives_hostile_comments_byte_for_byte(tmp_path):
    node_stub(tmp_path)
    for index, comment in enumerate(HOSTILE_COMMENTS):
        record = document(name=f"hostile {index}", source=comment + OK_SOURCE + "\n")
        files = THREE.files(record, {"credit": True})
        name = HEADER.pascal(record["name"])
        for filename, text in files.items():
            (tmp_path / filename).write_bytes(text.encode("utf-8"))
        runner = tmp_path / f"run{index}.mjs"
        runner.write_text(
            "import { readFileSync } from 'node:fs'\n"
            "try {\n"
            f"  const m = await import('./{name}Material.js')\n"
            f"  const frag = readFileSync(new URL('./{name}.frag.glsl', import.meta.url), 'utf8')\n"
            "  const material = m.create" + name + "Material()\n"
            "  console.log(JSON.stringify({ equal: m.fragmentShader === frag,"
            " same_in_material: material.fragmentShader === frag,"
            " pwned: globalThis.PWNED || null }))\n"
            "} catch (error) { console.log(JSON.stringify({ error: String(error) })) }\n",
            encoding="utf-8")
        proc = subprocess.run(["node", str(runner)], cwd=tmp_path, capture_output=True,
                              text=True, timeout=60)
        result = json.loads(proc.stdout.strip() or json.dumps({"stderr": proc.stderr[:400]}))
        assert result == {"equal": True, "same_in_material": True, "pwned": None}, (comment, result)


# Copy into dest_dir after the proof -------------------------------------------------------------

def seed(storage, record):
    db, base = storage
    conn = sqlite3.connect(db)
    RECORD.save(conn, record)
    conn.close()
    return base


@pytest.fixture
def fake_proof(monkeypatch):
    from shaderbruecke import exporting

    state = {"verdict": "differs", "calls": []}

    def prove(document, target_row, files, out_dir, **kwargs):
        state["calls"].append(target_row.id)
        return {"method": "fake", "verdict": state["verdict"], "why": "set by the test",
                "checks": [], "diff": None, "frames": []}

    monkeypatch.setattr(exporting, "prove", prove)
    return state


def test_export_copies_into_dest_dir_only_after_a_match(storage, fake_proof, tmp_path):
    seed(storage, document())
    dest = tmp_path / "project"
    dest.mkdir()

    refused = call(PLUGIN.shader_export, {"id": "sky", "target": "three", "dest_dir": str(dest)})
    assert refused["success"] is True and refused["proof"]["verdict"] == "differs"
    assert refused["copied_to"] == [] and list(dest.iterdir()) == []
    assert "accept_unproven" in refused["copy_skipped"]

    accepted = call(PLUGIN.shader_export, {"id": "sky", "target": "three", "dest_dir": str(dest),
                                           "accept_unproven": True})
    assert sorted(pathlib.Path(p).name for p in accepted["copied_to"]) == \
        ["GoldenHourSky.frag.glsl", "GoldenHourSkyMaterial.js"]
    assert accepted["copied_unproven"] is True and "differs" in accepted["warning"]

    fake_proof["verdict"] = "match"
    clean = tmp_path / "clean"
    clean.mkdir()
    matched = call(PLUGIN.shader_export, {"id": "sky", "target": "three", "dest_dir": str(clean)})
    assert len(matched["copied_to"]) == 2 and "copied_unproven" not in matched


def test_a_bad_dest_dir_is_refused_before_the_proof_runs(storage, fake_proof, tmp_path):
    seed(storage, document())
    result = call(PLUGIN.shader_export, {"id": "sky", "target": "three",
                                         "dest_dir": str(tmp_path / "missing")})
    assert result["success"] is False and "does not exist" in result["error"]
    assert fake_proof["calls"] == []


# Fork paths -------------------------------------------------------------------------------------

def test_fork_points_every_path_into_its_own_folder(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    base = tmp_path / "data"
    origin = RECORD.version_dir(base, "orig", 1)
    origin.mkdir(parents=True)
    for name in ("shader.frag", "frame-t0.png", "frame-t1.png", "contact-sheet.png"):
        (origin / name).write_text(name, encoding="utf-8")
    record = document(shader_id="orig")
    record["files"] = {"frag": str(origin / "shader.frag"),
                       "contact_sheet": str(origin / "contact-sheet.png"),
                       "snapshot": str(origin / "frame-t1.png")}
    record["checks"] = {"status": "ok", "contact_sheet": str(origin / "contact-sheet.png"),
                        "frames": [{"t": 0.0, "png_path": str(origin / "frame-t0.png")},
                                   {"t": 1.0, "png_path": str(origin / "frame-t1.png")}]}
    RECORD.save(conn, record)

    forked = RECORD.fork(conn, base, "orig", name="copy")
    own = RECORD.version_dir(base, forked["id"], 1)
    paths = list(forked["files"].values()) + [forked["checks"]["contact_sheet"]] + \
        [frame["png_path"] for frame in forked["checks"]["frames"]]
    for path in paths:
        assert pathlib.Path(path).is_absolute(), path
        assert pathlib.Path(path).parent == own, path
        assert pathlib.Path(path).is_file(), path


# The plugin folder stays clean ------------------------------------------------------------------

def plugin_tree():
    return {str(p.relative_to(ROOT)): p.stat().st_mtime_ns for p in ROOT.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


@pytest.mark.chromium
def test_a_harness_run_writes_nothing_into_the_plugin_folder(tmp_path):
    from shaderbruecke import renderer

    page = tmp_path / "page.html"
    page.write_text("<!doctype html><title>p</title>", encoding="utf-8")
    before = plugin_tree()
    value = renderer.run_page(page, "({ status: 'ok', two: 1 + 1 })", timeout_s=30.0)
    assert value.get("two") == 2, value
    assert plugin_tree() == before
    assert not (ROOT / "headless" / ".run").exists()


@pytest.mark.chromium
def test_a_page_reads_its_own_folder_and_no_other_local_file(tmp_path):
    """Catalog review #154066: the proof page runs code an imported shader shaped, so it gets no
    file:// access; it loads from its own folder over the DevTools pipe and nothing else."""
    from shaderbruecke import renderer

    site = tmp_path / "site"
    site.mkdir()
    (site / "page.html").write_text("<!doctype html><title>p</title>", encoding="utf-8")
    (site / "own.txt").write_text("own", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    probe = ("(async () => { const read = async url => { try { const r = await fetch(url);"
             " return r.ok ? await r.text() : 'status ' + r.status } catch (e) { return 'refused' } };"
             " return { secure: isSecureContext, own: await read('own.txt'),"
             f" file: await read({secret.as_uri()!r}), etc: await read('file:///etc/hostname'),"
             " up: await read('/..%2fsecret.txt'), dots: await read('/%2e%2e/secret.txt'),"
             " other: await read('https://example.com/') } })()")
    value = renderer.run_page(site / "page.html", probe, timeout_s=30.0)
    assert value.get("own") == "own" and value.get("secure") is True, value
    assert [value[k] for k in ("file", "etc", "up", "dots", "other")] == ["refused"] * 5, value
