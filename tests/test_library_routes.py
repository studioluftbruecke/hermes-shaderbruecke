"""The v0.2 library routes end to end through FastAPI against a temp library: the contract's shapes
(`experiments/shaderbruecke/v02-contract.md`), refusals with their codes, and D17's delete rule."""

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import record  # noqa: E402

SOURCE = ("// @slider glow 0.0..1.0 = 0.25\n"
          "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow); }\n")
ROW_KEYS = {"id", "name", "version", "versions", "states", "autoplay", "flash", "licence", "licenceInherited",
            "parent", "imported", "starred", "archived", "tags", "updatedAt", "createdAt", "authorKind", "thumb",
            "thumbVersion", "thumbStamp"}


def save(conn, base, shader_id, version, *, name="glow", source=SOURCE, thumb=True, **extra):
    folder = record.version_dir(base, shader_id, version)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shader.frag").write_text(source)
    files = {"frag": str(folder / "shader.frag")}
    if thumb:
        (folder / "thumb.webp").write_bytes(b"RIFFxxxxWEBP")
        files["thumb"] = str(folder / "thumb.webp")
    doc = {"id": shader_id, "version": version, "name": name, "source": source,
           "sliders": record.parse_sliders(source), "tuned_values": {}, "channels": [],
           "provenance": {"origin": "agent", "licence": None, "parent": None},
           "checks": {"status": "ok", "flat": False, "flash": {"verdict": "none"}, "motion": {"verdict": "moving"}},
           "edited_by": "agent", "author_kind": "agent", "created_at": f"2026-11-0{version}T10:00:00Z", "files": files}
    doc.update(extra)
    record.save(conn, doc)
    if version == 1:
        record.save_meta(conn, {**record.new_meta(shader_id, name), "updated_at": doc["created_at"]})
    return doc


@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from shaderbruecke.dashboard import plugin_api

    base = tmp_path / "data"
    base.mkdir()
    path = base / "data.db"
    seed = sqlite3.connect(path)
    record.ensure_schema(seed)
    save(seed, base, "a-sky", 1, name="sky \u2014 dusk")
    save(seed, base, "a-sky", 2, name="sky \u2014 dusk", source=SOURCE.replace("0.25", "0.5"))
    save(seed, base, "b-sea", 1, name="sea", thumb=False,
         checks={"status": "ok", "flat": False, "flash": {"verdict": "warn"}})
    seed.close()
    monkeypatch.setattr(plugin_api, "_storage",
                        lambda: (sqlite3.connect(path, check_same_thread=False), base))
    monkeypatch.setattr(plugin_api, "_connect", lambda: sqlite3.connect(path))
    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api")
    return TestClient(app)


def test_the_library_page_has_the_contract_shape(client):
    page = client.get("/api/library").json()
    assert set(page) == {"rows", "total", "cursor", "adoptable", "pickUp", "facets", "jobs"}
    assert page["total"] == 2 and page["cursor"] is None and page["adoptable"] == 0
    first = page["rows"][0]
    assert set(first) == ROW_KEYS
    assert first["id"] == "a-sky" and first["name"] == "sky - dusk" and first["versions"] == 2
    assert first["thumb"] is None and first["thumbVersion"] == 2 and first["autoplay"] is True
    sea = page["rows"][1]
    assert sea["states"] == ["flash_warn", "no_thumb"] and sea["autoplay"] is False and sea["thumbVersion"] is None
    batch = client.get("/api/thumbs?ids=a-sky@v2,b-sea@v1,Bad@v1,a-sky@v9").json()["thumbs"]
    assert batch["a-sky@v2"].startswith("data:image/webp;base64,")
    assert batch["b-sea@v1"] is None and batch["Bad@v1"] is None and batch["a-sky@v9"] is None
    assert client.get("/api/shaders/a-sky").json()["row"]["thumb"].startswith("data:image/webp")
    assert page["pickUp"]["id"] == "a-sky"
    assert client.get("/api/library?q=sea").json()["total"] == 1
    assert client.get("/api/library?limit=1").json()["cursor"] == "1"


def test_a_page_row_says_what_the_detail_row_says(client):
    # The page decodes records without source and sliders; the detail decodes them in full.
    rows = client.get("/api/library").json()["rows"]
    assert [row["id"] for row in rows] == ["a-sky", "b-sea"]
    for row in rows:
        assert {**client.get(f"/api/shaders/{row['id']}").json()["row"], "thumb": None} == row


def test_detail_version_and_compare(client):
    detail = client.get("/api/shaders/a-sky").json()
    assert set(detail) == {"row", "latest", "versions", "forks", "licenceOptions", "description"}
    assert [v["version"] for v in detail["versions"]] == [1, 2]
    assert detail["latest"]["effectiveValues"] == {"glow": 0.5}
    assert detail["licenceOptions"][0] == {"id": None, "label": "Private", "allowed": True, "reason": None,
                                           "meaning": detail["licenceOptions"][0]["meaning"]}
    version = client.get("/api/shaders/a-sky/versions/1").json()
    assert version["version"] == 1 and "0.25" in version["source"] and version["autoplay"] is True
    diff = client.get("/api/shaders/a-sky/compare?a=1&b=2").json()
    assert [l["op"] for l in diff["lines"]].count("-") == 1 and diff["sliders"][0]["change"] == "changed"
    assert client.get("/api/shaders/a-sky/versions/9").status_code == 404
    bad = client.get("/api/shaders/Bad_Id")
    assert bad.status_code == 400 and bad.json()["detail"]["error"] == "bad_id"


def test_patch_renames_tags_stars_and_restamps_the_licence(client):
    answer = client.patch("/api/shaders/a-sky", json={"name": "Dusk sky", "tags": ["Sky", "warm"],
                                                      "starred": True, "licence": "CC-BY-4.0"})
    assert answer.status_code == 200
    row = answer.json()["row"]
    assert row["name"] == "Dusk sky" and row["tags"] == ["sky", "warm"] and row["starred"] and row["licence"] == "CC-BY-4.0"
    versions = answer.json()["detail"]["versions"]
    assert [v["licence"] for v in versions] == [None, "CC-BY-4.0"]
    assert client.patch("/api/shaders/a-sky", json={"tags": ["x"] * 3 + [f"t{i}" for i in range(9)]}).status_code == 400


def test_an_inherited_licence_is_refused_with_409_and_the_allowed_list(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "data" / "data.db")
    record.update_meta(conn, "b-sea", licence="CC-BY-NC-SA-3.0", licence_inherited="CC-BY-NC-SA-3.0")
    conn.close()
    refused = client.patch("/api/shaders/b-sea", json={"licence": "MIT"})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["error"] == "licence_refused" and set(detail["allowed"]) == {"CC-BY-NC-SA-3.0", "CC-BY-NC-SA-4.0"}
    assert client.patch("/api/shaders/b-sea", json={"licence": "CC-BY-NC-SA-4.0"}).status_code == 200


def _pending_notices(tmp_path):
    conn = sqlite3.connect(tmp_path / "data" / "data.db")
    try:
        return [row[0] for row in conn.execute("SELECT id FROM human_notices WHERE delivered_at IS NULL")]
    finally:
        conn.close()


def _notice(tmp_path, shader_id, version):
    conn = sqlite3.connect(tmp_path / "data" / "data.db")
    record.add_human_notice(conn, shader_id, version)
    conn.close()


@pytest.mark.parametrize("archived", [True, False])
def test_delete_takes_any_shader_with_the_typed_name(client, tmp_path, archived):
    if archived:
        assert client.post("/api/shaders/b-sea/archive").json()["row"]["archived"] is True
    _notice(tmp_path, "b-sea", 1)
    project = tmp_path / "project" / "sea.frag"
    project.parent.mkdir()
    project.write_text("// an export the user copied into a project")
    wrong = client.request("DELETE", "/api/shaders/b-sea", json={"confirm": "Sea"})
    assert wrong.status_code == 400 and wrong.json()["detail"]["error"] == "confirm_mismatch"
    assert client.request("DELETE", "/api/shaders/b-sea", json={"confirm": "sea"}).json() == {"deleted": "b-sea"}
    assert client.get("/api/shaders/b-sea").status_code == 404
    assert not (tmp_path / "data" / "shaders" / "b-sea").exists()
    assert _pending_notices(tmp_path) == []
    assert project.read_text().startswith("// an export")
    assert client.request("DELETE", "/api/shaders/b-sea", json={"confirm": "sea"}).status_code == 404


def test_bulk_delete_checks_first_and_converges_on_a_repeat(client, tmp_path):
    body = {"ids": ["b-sea", "gone-one", "a-sky"], "confirm": "delete"}
    wrong = client.post("/api/library/delete", json={**body, "confirm": "Delete"})
    assert wrong.status_code == 400 and wrong.json()["detail"]["error"] == "confirm_mismatch"
    bad = client.post("/api/library/delete", json={**body, "ids": ["b-sea", "../etc"]})
    assert bad.status_code == 400 and bad.json()["detail"]["error"] == "bad_id"
    empty = client.post("/api/library/delete", json={"ids": [], "confirm": "delete"})
    assert empty.status_code == 400 and empty.json()["detail"]["error"] == "bad_request"
    assert client.get("/api/library").json()["total"] == 2, "a refused call deletes nothing"
    _notice(tmp_path, "a-sky", 2)
    assert client.post("/api/library/delete", json=body).json() == {"deleted": ["b-sea", "a-sky"],
                                                                     "missing": ["gone-one"]}
    assert client.get("/api/library").json()["total"] == 0
    assert not (tmp_path / "data" / "shaders" / "a-sky").exists()
    assert _pending_notices(tmp_path) == []
    assert client.post("/api/library/delete", json=body).json() == {"deleted": [],
                                                                     "missing": ["b-sea", "gone-one", "a-sky"]}


def test_library_ids_lists_every_id_the_paged_query_returns(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "data" / "data.db")
    for n in range(7):
        save(conn, tmp_path / "data", f"c-wave-{n}", 1, name=f"wave {n}")
    conn.close()
    client.post("/api/shaders/c-wave-3/archive")
    for query in ("", "sort=name", "q=wave", "archived=1", "q=zzz"):
        paged, cursor = [], "0"
        while cursor is not None:
            page = client.get(f"/api/library?{query}&limit=3&cursor={cursor}").json()
            paged += [row["id"] for row in page["rows"]]
            cursor = page["cursor"]
        answer = client.get(f"/api/library/ids?{query}&limit=3&cursor=3").json()
        assert answer == {"ids": paged, "total": len(paged)}, query
    assert client.get("/api/library/ids?q=wave").json()["total"] == 6


def test_settings_route_answers_the_starter_prompt_switch(client, monkeypatch):
    from shaderbruecke import settings
    assert client.get("/api/settings").json() == {"starter_prompt": True}
    monkeypatch.setattr(settings, "_raw_settings", lambda: {"starter_prompt": False})
    assert client.get("/api/settings").json() == {"starter_prompt": False}
    monkeypatch.setattr(settings, "_raw_settings", lambda: {"starter_prompt": "on"})
    assert client.get("/api/settings").json() == {"starter_prompt": True}


def test_unarchive_brings_a_shader_back(client):
    client.post("/api/shaders/b-sea/archive")
    assert client.post("/api/shaders/b-sea/unarchive").json()["row"]["archived"] is False


def test_fork_of_a_version_and_its_lineage(client):
    forked = client.post("/api/shaders/a-sky/fork", json={"version": 1, "name": "sky fork"}).json()["row"]
    assert forked["name"] == "sky fork" and forked["parent"] == {"id": "a-sky", "name": "sky - dusk", "version": 1}
    assert client.get("/api/shaders/a-sky").json()["forks"] == [{"id": forked["id"], "name": "sky fork", "version": 1}]
    assert client.post("/api/shaders/a-sky/fork", json={"version": 7}).status_code == 404


def test_jobs_start_and_report(client):
    job = client.post("/api/jobs/thumbs").json()["job"]
    assert job["kind"] == "thumbs" and job["total"] == 1
    assert client.get(f"/api/jobs/{job['id']}").json()["job"]["id"] == job["id"]
    adopt = client.post("/api/jobs/adopt").json()["job"]
    assert adopt["kind"] == "adopt" and adopt["result"]["skipped"] == [] and adopt["total"] == 0
    assert client.post("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_a_rerendered_thumb_gets_a_new_stamp_without_a_new_version(client, tmp_path):
    row = client.get("/api/library").json()["rows"][0]
    assert row["id"] == "a-sky" and isinstance(row["thumbStamp"], int)
    thumb = record.version_dir(tmp_path / "data", "a-sky", 2) / "thumb.webp"
    thumb.write_bytes(b"RIFFyyyyWEBP")
    import os
    os.utime(thumb, ns=(thumb.stat().st_atime_ns, thumb.stat().st_mtime_ns + 5_000_000_000))
    again = client.get("/api/library").json()["rows"][0]
    assert again["thumbStamp"] > row["thumbStamp"] and again["updatedAt"] == row["updatedAt"]
    assert client.get("/api/library").json()["rows"][1]["thumbStamp"] is None


def test_the_detail_filmstrip_carries_stamps_not_images(client):
    detail = client.get("/api/shaders/a-sky").json()
    for summary in detail["versions"]:
        assert summary["thumb"] is None and isinstance(summary["thumbStamp"], int)
        assert summary["autoplay"] is True and summary["flash"] == "none"
    assert detail["latest"]["thumb"].startswith("data:image/webp")
    sea = client.get("/api/shaders/b-sea").json()["versions"][0]
    assert sea["thumbStamp"] is None and sea["autoplay"] is False and sea["flash"] == "warn"
