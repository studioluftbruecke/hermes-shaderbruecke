"""The plugin API: registry JSON, export text, slider values, and the FastAPI routes."""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402


def make_db(tmp_path, shader_id="s1", name="golden-hour sky"):
    conn = sqlite3.connect(tmp_path / "data.db", check_same_thread=False)  # TestClient runs endpoints in a threadpool
    document = {"id": shader_id, "version": 1, "name": name,
                "source": "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }",
                "sliders": [{"name": "gain", "min": 0.0, "max": 2.0, "default": 1.0, "step": None}],
                "slider_values": {"gain": 1.0}, "channels": [],
                "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
                "checks": {"status": "ok"}, "edited_by": "agent",
                "created_at": "2026-11-08T00:00:00Z", "files": {}}
    RECORD.save(conn, document)
    return conn, tmp_path / "data"


def test_targets_lists_the_registry_and_what_is_exported(tmp_path):
    conn, base = make_db(tmp_path)
    payload = library_api.targets_for(conn, base, "s1")
    ids = [row["id"] for row in payload["targets"]]
    assert ids == ["three", "tsl", "r3f", "agent", "shadertoy"]
    assert all(row["exported"] is False for row in payload["targets"])
    exported = RECORD.version_dir(base, "s1", 1) / "exports" / "three"
    exported.mkdir(parents=True)
    (exported / "GoldenHourSkyMaterial.js").write_text("// x", encoding="utf-8")
    again = library_api.targets_for(conn, base, "s1")
    three = next(row for row in again["targets"] if row["id"] == "three")
    assert three["exported"] is True


def test_export_text_reads_the_file_and_reports_a_missing_export(tmp_path):
    conn, base = make_db(tmp_path)
    missing = library_api.export_text(conn, base, "s1", "three")
    assert missing["needs_export"] is True
    directory = RECORD.version_dir(base, "s1", 1) / "exports" / "three"
    directory.mkdir(parents=True)
    (directory / "GoldenHourSkyMaterial.js").write_text("export const x = 1\n", encoding="utf-8")
    found = library_api.export_text(conn, base, "s1", "three")
    assert found["text"].startswith("export const x")


def test_agent_export_is_generated_without_a_file(tmp_path):
    conn, base = make_db(tmp_path)
    payload = library_api.export_text(conn, base, "s1", "agent")
    assert payload["main_file"] == "GoldenHourSky.handoff.md"
    assert payload["text"].startswith("# Shaderbrücke handoff")


def test_unknown_ids_and_targets_are_errors(tmp_path):
    conn, base = make_db(tmp_path)
    assert "no shader" in library_api.targets_for(conn, base, "nope")["error"]
    assert "unknown target" in library_api.export_text(conn, base, "s1", "nope")["error"]


def test_slider_values_round_trip(tmp_path):
    conn, _base = make_db(tmp_path)
    updated = library_api.set_values(conn, "s1", {"gain": 1.7, "bogus": 3.0})
    assert updated["slider_values"] == {"gain": 1.7}
    assert RECORD.latest(conn, "s1")["edited_by"] == "human"


def test_router_routes_reach_the_logic(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from shaderbruecke.dashboard import plugin_api

    conn, base = make_db(tmp_path)
    monkeypatch.setattr(plugin_api, "_storage", lambda: (conn, base))
    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/shaderbruecke")
    client = TestClient(app)

    listed = client.get("/api/plugins/shaderbruecke/shaders/s1/targets")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["targets"]][0] == "three"

    assert client.get("/api/plugins/shaderbruecke/shaders/s1/export/three").status_code == 409
    handoff = client.get("/api/plugins/shaderbruecke/shaders/s1/export/agent")
    assert handoff.status_code == 200 and handoff.json()["text"].startswith("# Shaderbrücke handoff")

    posted = client.post("/api/plugins/shaderbruecke/shaders/s1/slider_values", json={"values": {"gain": 0.5}})
    assert posted.status_code == 200 and posted.json()["slider_values"] == {"gain": 0.5}
    assert client.get("/api/plugins/shaderbruecke/shaders/nope/targets").status_code == 404
