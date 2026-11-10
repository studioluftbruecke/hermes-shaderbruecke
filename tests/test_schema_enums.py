"""Guardrail plugins guard their own code: every action a tool schema offers is one its handler
accepts (and nothing else), and the organising actions do what they say against a temp library."""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke  # noqa: E402
from shaderbruecke import record  # noqa: E402
from shaderbruecke.exporters import registry  # noqa: E402

SOURCE = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }\n"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    record.ensure_schema(conn)
    for index, shader_id in enumerate(("a-sky", "b-sea")):
        record.save(conn, {"id": shader_id, "version": 1, "name": shader_id[2:], "source": SOURCE, "sliders": [],
                           "tuned_values": {}, "provenance": {"origin": "agent", "licence": None, "parent": None},
                           "checks": {"status": "ok", "flash": {"verdict": "none"}}, "author_kind": "agent",
                           "created_at": f"2026-11-0{index + 1}T00:00:00Z", "files": {}})
    conn.close()
    monkeypatch.setattr(shaderbruecke, "_storage", lambda: (sqlite3.connect(path), tmp_path))
    return tmp_path


def call(**args):
    return json.loads(shaderbruecke.shader_library(args))


def test_the_library_schema_enum_is_exactly_the_handled_actions(lib, monkeypatch):
    monkeypatch.setattr(shaderbruecke, "render_version", lambda *a, **k: {"success": False, "error": "no render here"})
    enum = shaderbruecke.SHADER_LIBRARY["parameters"]["properties"]["action"]["enum"]
    assert enum == list(shaderbruecke.LIBRARY_ACTIONS)
    for action in enum:
        answer = call(action=action, id="a-sky", source=SOURCE)
        assert "unknown action" not in str(answer.get("error")), action
    assert call(action="delete", id="a-sky")["error"] == "unknown action delete"


def test_the_export_schema_enum_is_the_registry():
    assert shaderbruecke.SHADER_EXPORT["parameters"]["properties"]["target"]["enum"] == registry.target_ids()


def test_rename_tag_archive_and_unarchive(lib):
    assert call(action="rename", id="a-sky", name="  Dusk   sky ")["name"] == "Dusk sky"
    got = call(action="get", id="a-sky")["shader"]
    assert got["name"] == "Dusk sky" and got["record_name"] == "sky"
    assert call(action="rename", id="a-sky", name=" ")["success"] is False
    assert call(action="tag", id="a-sky", tags=["Warm", "sky"])["tags"] == ["warm", "sky"]
    assert "at most 8" in call(action="tag", id="a-sky", tags=[f"t{i}" for i in range(9)])["error"]
    listed = call(action="list")
    assert [s["name"] for s in listed["shaders"]] == ["Dusk sky", "sea"] and listed["total"] == 2
    assert call(action="list", tag="warm")["shaders"][0]["id"] == "a-sky"
    assert call(action="archive", id="a-sky")["archived"] is True
    assert [s["id"] for s in call(action="list")["shaders"]] == ["b-sea"]
    assert [s["id"] for s in call(action="list", archived=True)["shaders"]] == ["a-sky"]
    assert call(action="unarchive", id="a-sky")["archived"] is False
    assert call(action="archive", id="nope")["success"] is False


def test_list_pages_with_a_cursor(lib):
    first = call(action="list", limit=1)
    assert first["count"] == 1 and first["cursor"] == "1"
    assert call(action="list", limit=1, cursor=first["cursor"])["cursor"] is None
