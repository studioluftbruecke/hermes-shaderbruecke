"""Share files (format shaderbruecke/1): build from a record, parse and validate, and the import
path that keeps claims as provenance and an NC or SA licence as inherited. The render is faked."""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke  # noqa: E402
from shaderbruecke import library, record  # noqa: E402

SOURCE = ("// @slider glow 0.0..1.0 = 0.25 \"Glow\"\n"
          "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow); }\n")


def doc(**extra):
    base = {"id": "a-glow", "version": 2, "name": "glow", "source": SOURCE, "sliders": record.parse_sliders(SOURCE),
            "tuned_values": {"glow": 0.4}, "provenance": {"origin": "agent", "licence": "CC-BY-4.0", "parent": None},
            "checks": {"status": "ok", "flash": {"verdict": "none"}}, "author_kind": "agent",
            "created_at": "2026-11-09T00:00:00Z", "files": {}}
    base.update(extra)
    return base


def share(**extra):
    meta = {**record.new_meta("a-glow", "Glow \u2014 soft"), "tags": ["light"], "description": "soft glow"}
    built = library.build_share(doc(), meta, kinds=["agent", "human"],
                                settings={"author_name": "Ada", "author_url": "https://ada.example"},
                                thumb="data:image/webp;base64,AA", parent=None, made_with="shaderbruecke 0.2.0")
    built.update(extra)
    return built


def test_a_share_file_has_the_fixed_format():
    built = share()
    assert list(built) == ["format", "name", "source", "sliders", "tuned_values", "licence", "licence_inherited",
                           "author", "ai_involvement", "third_party", "lineage", "tags", "description", "thumb",
                           "made_with"]
    assert built["format"] == "shaderbruecke/1" and built["name"] == "Glow - soft"
    assert built["sliders"] == [{"name": "glow", "min": 0.0, "max": 1.0, "default": 0.25, "step": None, "label": "Glow"}]
    assert built["tuned_values"] == {"glow": 0.4} and built["licence"] == "CC-BY-4.0"
    assert built["author"] == {"name": "Ada", "url": "https://ada.example"} and built["ai_involvement"] == "assisted"
    json.dumps(built)


@pytest.mark.parametrize("kinds, expected", [(["agent"], "generated"), (["agent", "human"], "assisted"),
                                             (["human"], "none"), (["import"], "unknown")])
def test_ai_involvement_follows_who_wrote_the_versions(kinds, expected):
    assert library.ai_involvement(kinds) == expected


def test_parse_keeps_claims_and_drops_what_the_importer_rebuilds():
    parsed, why = library.parse_share(share(sliders=[{"name": "evil"}], thumb="data:x", extra_field=1))
    assert why is None
    assert parsed["source"] == SOURCE and parsed["licence"] == "CC-BY-4.0" and parsed["author"] == "Ada"
    assert "sliders" not in parsed and "thumb" not in parsed and parsed["tags"] == ["light"]


@pytest.mark.parametrize("change, reason", [
    ({"format": "shaderbruecke/2"}, "unknown format"),
    ({"source": ""}, "no source"),
    ({"source": "x" * (64 * 1024 + 1)}, "larger than"),
    ({"licence": "Made-Up-1.0"}, "unknown licence"),
])
def test_parse_refuses_a_bad_share_file(change, reason):
    parsed, why = library.parse_share(share(**change))
    assert parsed is None and reason in why
    assert library.parse_share([1, 2])[1] == "a share file is a JSON object"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "data.db")
    record.ensure_schema(conn)
    calls = []

    def fake_render_version(conn_, base_, **kwargs):
        calls.append(kwargs)
        if "BROKEN" in kwargs["source"]:
            return {"success": False, "error": "compile error: 1 diagnostic(s)",
                    "errors": [{"line": 2, "where": "shader", "message": "'BROKEN' : undeclared identifier"}]}
        shader_id = record.new_id(kwargs["name"])
        meta = {**record.new_meta(shader_id, kwargs["name"]), **(kwargs.get("meta_defaults") or {})}
        record.save_meta(conn_, meta)
        record.save(conn_, {"id": shader_id, "version": 1, "name": kwargs["name"], "source": kwargs["source"],
                            "sliders": record.parse_sliders(kwargs["source"]), "tuned_values": {},
                            "provenance": {"origin": "agent", "licence": meta["licence"], **kwargs["provenance"]},
                            "checks": {"status": "ok", "flash": {"verdict": "none"}},
                            "author_kind": kwargs["author_kind"], "created_at": "2026-11-09T00:00:00Z",
                            "files": {"frag": "/x/shader.frag"}})
        return {"success": True, "id": shader_id, "version": 1}

    monkeypatch.setattr(shaderbruecke, "render_version", fake_render_version)
    yield conn, tmp_path, calls
    conn.close()


def test_a_share_file_round_trips_its_claims(lib):
    conn, base, calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"share": share(licence="CC-BY-SA-4.0", lineage=[
        {"name": "Late ember", "author": "Bo", "licence": "MIT", "url": None}])})
    assert answer["ok"] and answer["dialect"] == "shaderbruecke"
    newest = record.latest(conn, answer["id"])
    assert newest["author_kind"] == "import" and newest["provenance"]["origin"] == "import"
    assert newest["provenance"]["author"] == "Ada" and newest["provenance"]["lineage"][0]["name"] == "Late ember"
    assert newest["tuned_values"] == {"glow": 0.4}
    meta = record.get_meta(conn, answer["id"])
    assert meta["licence"] == "CC-BY-SA-4.0" and meta["licence_inherited"] == "CC-BY-SA-4.0"
    assert any("CC-BY-SA-4.0" in warning for warning in answer["warnings"])
    assert calls[0]["author_kind"] == "import"


def test_a_pasted_shadertoy_shader_without_a_licence_gets_shadertoys_default_and_keeps_it(lib):
    conn, base, _calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"source": SOURCE, "name": "Neon rain",
                                                      "url": "https://www.shadertoy.com/view/xyz", "author": "demo"})
    assert answer["ok"] and answer["licence"] == "CC-BY-NC-SA-3.0"
    assert any("CC BY-NC-SA 3.0" in w for w in answer["warnings"])
    assert any("may not be used commercially" in w for w in answer["warnings"])
    meta = record.get_meta(conn, answer["id"])
    assert meta["licence_inherited"] == "CC-BY-NC-SA-3.0"
    assert library.licence_change_refusal(meta, "MIT") is not None
    assert record.latest(conn, answer["id"])["provenance"]["url"] == "https://www.shadertoy.com/view/xyz"


def test_a_failing_import_lands_nothing_and_names_the_line(lib):
    conn, base, _calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"source": SOURCE.replace("vec4(glow)", "vec4(BROKEN)"),
                                                      "licence": "MIT"})
    assert answer == {"ok": False, "error": "compile_error", "message": "compile error: 1 diagnostic(s)",
                      "diagnostics": [{"line": 2, "severity": "error", "message": "'BROKEN' : undeclared identifier"}],
                      "dialect": "shadertoy"}
    assert record.list_recent(conn) == []


def test_unsupported_inputs_are_refused_before_any_render(lib):
    conn, base, calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"source": SOURCE.replace("vec4(glow)", "texture(iChannel0, fragCoord)")})
    assert answer["ok"] is False and answer["error"] == "not_supported" and calls == []


def test_a_claimed_link_must_be_http_or_https():
    parsed, why = library.parse_share(share(author={"name": "Ada", "url": "javascript:alert(1)"}, lineage=[
        {"name": "x", "url": "data:text/html,hi"}, {"name": "y", "url": "https://example.org/y"}]))
    assert why is None and parsed["author_url"] is None
    assert parsed["lineage"][0]["url"] is None and parsed["lineage"][1]["url"] == "https://example.org/y"


def test_a_share_file_keeps_its_authors_link_apart_from_the_original_url(lib):
    conn, base, _calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"share": share()})
    provenance = record.latest(conn, answer["id"])["provenance"]
    assert provenance["author_url"] == "https://ada.example" and provenance["url"] is None


@pytest.mark.parametrize("licence", ["CC-BY-ND-4.0", "CC-BY-NC-ND-4.0"])
def test_a_no_derivatives_licence_is_refused_on_import(lib, licence):
    conn, base, calls = lib
    pasted = shaderbruecke.import_shader(conn, base, {"source": SOURCE, "licence": licence})
    shared, why = library.parse_share(share(licence=licence))
    assert pasted["ok"] is False and "ND" in pasted["message"] and calls == []
    assert shared is None and "ND" in why


def test_a_pasted_url_must_be_http_or_https(lib):
    conn, base, _calls = lib
    answer = shaderbruecke.import_shader(conn, base, {"source": SOURCE, "licence": "MIT", "url": "javascript:x"})
    assert record.latest(conn, answer["id"])["provenance"]["url"] is None
