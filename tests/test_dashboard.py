"""The plugin API: registry JSON with proofs, export text and export-from-the-card, slider values
(set and reset), id validation, and the FastAPI routes.

Some record/exporting/settings names come from the v0.1.1 server half (`record.valid_id`,
`record.effective_values`, `record.reset_tuned_values`, `exporting.read_proof`,
`exporting.export_shader`, `settings.read_settings`). Until both halves are merged, the autouse
fixture stands in for the record names that are missing, and each test replaces the export and
settings seams it needs; `test_contract_names_exist` fails until the server half is there.
"""

import importlib
import pathlib
import re
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402
from shaderbruecke.exporters.header import CREDIT  # noqa: E402

REAL_EXPORTING = library_api._exporting
REAL_SETTINGS = library_api._settings

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
# Read before any fixture stands in for them.
RECORD_NAMES_MISSING = [n for n in ("valid_id", "effective_values", "reset_tuned_values", "set_slider_values")
                        if not callable(getattr(RECORD, n, None))]


@pytest.fixture(autouse=True)
def contract(monkeypatch):
    """Stand-ins for the server half's record names, used only where the real ones are missing."""
    def effective_values(doc):
        tuned = doc.get("tuned_values") or {}
        return {s["name"]: float(tuned.get(s["name"], s["default"])) for s in doc.get("sliders") or []}

    def reset_tuned_values(conn, shader_id):
        doc = RECORD.latest(conn, shader_id)
        if doc is None:
            return None
        doc["tuned_values"] = {}
        return RECORD.save(conn, doc)

    for name, stub in (("valid_id", lambda s: bool(ID_RE.match(s or ""))),
                       ("effective_values", effective_values),
                       ("reset_tuned_values", reset_tuned_values)):
        if not hasattr(RECORD, name):
            monkeypatch.setattr(RECORD, name, stub, raising=False)
    exports = SimpleNamespace(calls=[], proofs={})
    exports.read_proof = lambda base, shader_id, version, target: exports.proofs.get(target)

    def export_shader(conn, base, shader_id, target, **kwargs):
        exports.calls.append({"id": shader_id, "target": target, **kwargs})
        return {"success": True, "id": shader_id, "version": 1, "target": target,
                "proof": {"method": "three-harness", "verdict": "match", "why": "same frame"}}

    exports.export_shader = export_shader
    settings = {"allow_project_writes": False, "credit_line": True, "keep_versions": 5}
    monkeypatch.setattr(library_api, "_exporting", lambda: exports)
    monkeypatch.setattr(library_api, "_settings", lambda: dict(settings))
    return SimpleNamespace(exports=exports, settings=settings)


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


def export_file(base, target="three", name="GoldenHourSkyMaterial.js", text="// x"):
    directory = RECORD.version_dir(base, "s1", 1) / "exports" / target
    directory.mkdir(parents=True)
    (directory / name).write_text(text, encoding="utf-8")


def test_contract_names_exist():
    # Fails until the v0.1.1 server half is merged: the routes call these names.
    missing = [f"record.{n}" for n in RECORD_NAMES_MISSING]
    for module, names in (("shaderbruecke.exporting", ("export_shader", "read_proof")),
                          ("shaderbruecke.settings", ("read_settings",))):
        try:
            loaded = importlib.import_module(module)
        except ImportError:
            missing.append(module)
            continue
        missing += [f"{module}.{n}" for n in names if not callable(getattr(loaded, n, None))]
    assert not missing, f"server-half names missing: {missing}"


def test_targets_lists_the_registry_what_is_exported_and_the_proof(tmp_path, contract):
    conn, base = make_db(tmp_path)
    payload = library_api.targets_for(conn, base, "s1")
    ids = [row["id"] for row in payload["targets"]]
    assert ids == ["three", "tsl", "r3f", "agent", "shadertoy", "td", "isf"]
    assert all(row["exported"] is False and row["proof"] is None for row in payload["targets"])
    assert payload["name"] == "golden-hour sky" and payload["version"] == 1
    assert [s["name"] for s in payload["sliders"]] == ["gain"]
    assert payload["tuned_values"] == {"gain": 1.0}
    assert payload["targets"][0]["proof_method"] == "three-harness"
    export_file(base)
    contract.exports.proofs["three"] = {"method": "three-harness", "verdict": "differs", "why": "mean 0.03",
                                        "checks": [], "frames": []}
    again = library_api.targets_for(conn, base, "s1")
    three = next(row for row in again["targets"] if row["id"] == "three")
    assert three["exported"] is True
    assert three["proof"] == {"verdict": "differs", "why": "mean 0.03"}


def test_an_export_without_a_stored_proof_has_proof_none(tmp_path):
    conn, base = make_db(tmp_path)
    export_file(base)
    three = library_api.targets_for(conn, base, "s1")["targets"][0]
    assert three["exported"] is True and three["proof"] is None


def test_export_text_reads_the_file_and_reports_a_missing_export(tmp_path):
    conn, base = make_db(tmp_path)
    missing = library_api.export_text(conn, base, "s1", "three")
    assert missing["needs_export"] is True and missing["status"] == 409
    export_file(base, text="export const x = 1\n")
    found = library_api.export_text(conn, base, "s1", "three")
    assert found["text"].startswith("export const x")


def test_agent_export_is_generated_without_a_file_and_honours_credit_line(tmp_path, contract):
    conn, base = make_db(tmp_path)
    payload = library_api.export_text(conn, base, "s1", "agent")
    assert payload["main_file"] == "GoldenHourSky.handoff.md"
    assert payload["text"].startswith("# Shaderbrücke handoff")
    assert CREDIT in payload["text"]
    contract.settings["credit_line"] = False
    assert CREDIT not in library_api.export_text(conn, base, "s1", "agent")["text"]


def test_agent_copy_serves_the_exported_handoff_when_it_exists(tmp_path, contract):
    # The exported handoff carries the measured acceptance grid; a regenerated one does not.
    conn, base = make_db(tmp_path)
    export_file(base, target="agent", name="GoldenHourSky.handoff.md", text="# measured handoff\n")
    assert library_api.export_text(conn, base, "s1", "agent")["text"] == "# measured handoff\n"


@pytest.mark.chromium
def test_export_now_runs_the_real_exporter_and_the_card_sees_the_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(library_api, "_exporting", REAL_EXPORTING)
    monkeypatch.setattr(library_api, "_settings", REAL_SETTINGS)
    conn, base = make_db(tmp_path)
    payload = library_api.export_now(conn, base, "s1", "three")
    assert payload["success"] is True, payload
    assert payload["proof"]["verdict"] == "match", payload["proof"]
    rows = {row["id"]: row for row in library_api.targets_for(conn, base, "s1")["targets"]}
    assert rows["three"]["exported"] is True
    assert rows["three"]["proof"]["verdict"] == "match"
    copied = library_api.export_text(conn, base, "s1", "three")
    assert "createGoldenHourSkyMaterial" in copied["text"]


def test_export_now_runs_the_exporter_with_the_settings(tmp_path, contract):
    conn, base = make_db(tmp_path)
    contract.settings["credit_line"] = False
    payload = library_api.export_now(conn, base, "s1", "three")
    assert payload["success"] is True and payload["proof"]["verdict"] == "match"
    call = contract.exports.calls[-1]
    assert call["id"] == "s1" and call["target"] == "three" and call["version"] is None
    assert call["settings"]["credit_line"] is False
    assert "dest_dir" not in call  # nothing is written into a project from the card
    library_api.export_now(conn, base, "s1", "tsl", version=1)
    assert contract.exports.calls[-1]["version"] == 1


def test_unknown_ids_targets_and_bad_ids_are_errors(tmp_path, contract):
    conn, base = make_db(tmp_path)
    assert library_api.targets_for(conn, base, "nope")["status"] == 404
    assert library_api.export_text(conn, base, "s1", "nope")["status"] == 404
    assert library_api.export_now(conn, base, "s1", "nope")["status"] == 404
    assert library_api.export_now(conn, base, "nope", "three")["status"] == 404
    for bad in ("../x", "Bad_Id", "", "a/b"):
        assert library_api.targets_for(conn, base, bad)["status"] == 400
        assert library_api.export_text(conn, base, bad, "three")["status"] == 400
        assert library_api.export_now(conn, base, bad, "three")["status"] == 400
        assert library_api.set_values(conn, base, bad, {"gain": 1.0})["status"] == 400
        assert library_api.reset_values(conn, base, bad)["status"] == 400
    assert contract.exports.calls == []


def test_slider_values_round_trip_and_reset(tmp_path):
    conn, base = make_db(tmp_path)
    updated = library_api.set_values(conn, base, "s1", {"gain": 1.7, "bogus": 3.0})
    assert updated["tuned_values"] == {"gain": 1.7}
    assert RECORD.latest(conn, "s1")["edited_by"] == "human"
    assert library_api.targets_for(conn, base, "s1")["tuned_values"] == {"gain": 1.7}
    reset = library_api.reset_values(conn, base, "s1")
    assert reset["tuned_values"] == {}
    assert library_api.targets_for(conn, base, "s1")["effective_values"] == {"gain": 1.0}
    assert library_api.reset_values(conn, base, "nope")["status"] == 404


def test_router_routes_reach_the_logic(tmp_path, monkeypatch, contract):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from shaderbruecke.dashboard import plugin_api

    conn, base = make_db(tmp_path)
    conn.close()  # each route opens its own connection and closes it
    monkeypatch.setattr(plugin_api, "_storage",
                        lambda: (sqlite3.connect(tmp_path / "data.db", check_same_thread=False), base))
    monkeypatch.setattr(plugin_api, "_connect", lambda: sqlite3.connect(tmp_path / "data.db"))
    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/shaderbruecke")
    client = TestClient(app)
    api = "/api/plugins/shaderbruecke/shaders"

    listed = client.get(f"{api}/s1/targets")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["targets"]][0] == "three"
    assert client.get(f"{api}/s1/targets?version=1").status_code == 200

    missing = client.get(f"{api}/s1/export/three")
    assert missing.status_code == 409 and "not been exported" in missing.json()["detail"]
    handoff = client.get(f"{api}/s1/export/agent")
    assert handoff.status_code == 200 and handoff.json()["text"].startswith("# Shaderbrücke handoff")

    exported = client.post(f"{api}/s1/export/three", json={})
    assert exported.status_code == 200 and exported.json()["proof"]["verdict"] == "match"
    assert contract.exports.calls[-1]["target"] == "three"
    assert client.post(f"{api}/s1/export/nope").status_code == 404

    posted = client.post(f"{api}/s1/slider_values", json={"values": {"gain": 0.5}})
    assert posted.status_code == 200 and posted.json()["tuned_values"] == {"gain": 0.5}
    deleted = client.delete(f"{api}/s1/slider_values")
    assert deleted.status_code == 200 and deleted.json()["tuned_values"] == {}

    assert client.get(f"{api}/nope/targets").status_code == 404
    for method, path in (("get", "Bad_Id/targets"), ("get", "Bad_Id/export/three"), ("post", "Bad_Id/export/three"),
                         ("delete", "Bad_Id/slider_values")):
        assert getattr(client, method)(f"{api}/{path}").status_code == 400, path
    assert client.post(f"{api}/Bad_Id/slider_values", json={"values": {}}).status_code == 400
