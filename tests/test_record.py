"""`@slider` parsing, version storage and fork lineage — with no Hermes import anywhere."""

import importlib.util
import sqlite3
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_record():
    spec = importlib.util.spec_from_file_location("shaderbruecke_record", ROOT / "record.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECORD = load_record()


def test_parses_min_max_default_and_step():
    source = ("// @slider cloudCover 0.0..1.0 = 0.55\n"
              "// @slider sunHeight 0.05..0.95 = 0.32 step 0.01\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { }")
    assert RECORD.parse_sliders(source) == [
        {"name": "cloudCover", "min": 0.0, "max": 1.0, "default": 0.55, "step": None},
        {"name": "sunHeight", "min": 0.05, "max": 0.95, "default": 0.32, "step": 0.01},
    ]


def test_ignores_bad_names_reserved_names_and_bad_ranges():
    source = ("// @slider 2fast 0.0..1.0 = 0.5\n"
              "// @slider iTime 0.0..1.0 = 0.5\n"
              "// @slider backwards 1.0..0.0 = 0.5\n"
              "// @slider outside 0.0..1.0 = 2.0\n"
              "// @slider ok 0.0..1.0 = 0.5\n"
              "// @slider ok 0.2..0.8 = 0.5\n"
              "void mainImage(out vec4 fragColor, in vec2 fragCoord) { }")
    assert [s["name"] for s in RECORD.parse_sliders(source)] == ["ok"]
    assert RECORD.parse_sliders(source)[0]["default"] == 0.5


def test_caps_at_sixteen_sliders():
    source = "".join(f"// @slider s{i} 0.0..1.0 = 0.5\n" for i in range(20))
    assert len(RECORD.parse_sliders(source)) == RECORD.MAX_SLIDERS == 16


def test_header_and_defaults():
    sliders = RECORD.parse_sliders("// @slider gain 0.0..2.0 = 1.0\n")
    assert RECORD.slider_header(sliders) == "uniform float gain;\n"
    assert RECORD.slider_values(sliders) == {"gain": 1.0}


def _record(shader_id, version, name="test", source="void mainImage(out vec4 c, in vec2 p) {}"):
    return {"id": shader_id, "version": version, "name": name, "source": source,
            "sliders": [], "slider_values": {}, "channels": [],
            "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
            "checks": {"status": "ok"}, "edited_by": "agent",
            "created_at": f"2026-11-08T0{version}:00:00Z", "files": {"frag": f"/tmp/{shader_id}/v{version}/shader.frag"}}


def test_versions_round_trip(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    RECORD.save(conn, _record("a", 1))
    RECORD.save(conn, _record("a", 2, name="second"))
    assert RECORD.next_version(conn, "a") == 3
    assert RECORD.latest(conn, "a")["version"] == 2
    assert RECORD.get_version(conn, "a", 1)["name"] == "test"
    assert RECORD.get_version(conn, "a", 9) is None
    assert [r["version"] for r in RECORD.history(conn, "a")] == [1, 2]
    RECORD.save(conn, _record("b", 1, name="other"))
    listed = RECORD.list_recent(conn, 20)
    assert {row["id"] for row in listed} == {"a", "b"}
    assert listed[0]["id"] == "a"          # newest created_at first


def test_fork_keeps_lineage_and_copies_files(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    base = tmp_path / "data"
    version_dir = RECORD.version_dir(base, "a", 1)
    version_dir.mkdir(parents=True)
    (version_dir / "shader.frag").write_text("// original", encoding="utf-8")
    RECORD.save(conn, _record("a", 1))

    forked = RECORD.fork(conn, base, "a", name="variant")
    assert forked["id"] != "a"
    assert forked["version"] == 1
    assert forked["provenance"]["parent"] == "a"
    assert forked["name"] == "variant"
    copied = RECORD.version_dir(base, forked["id"], 1) / "shader.frag"
    assert copied.read_text(encoding="utf-8") == "// original"
    assert RECORD.latest(conn, "a")["version"] == 1     # the parent is untouched


def test_slider_values_merge_only_known_names(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    document = _record("a", 1)
    document["sliders"] = [{"name": "gain", "min": 0.0, "max": 2.0, "default": 1.0, "step": None}]
    document["slider_values"] = {"gain": 1.0}
    RECORD.save(conn, document)
    updated = RECORD.set_slider_values(conn, tmp_path / "data", "a", {"gain": 1.4, "nope": 3.0})
    assert updated["slider_values"] == {"gain": 1.4}
    assert updated["edited_by"] == "human"
