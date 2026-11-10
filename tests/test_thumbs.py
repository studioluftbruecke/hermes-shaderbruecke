"""Thumbnails (F36), meta and licence stamping, forks and adopting flat folders, with a fake
renderer: no browser and no Hermes."""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import record, renderer, thumbs  # noqa: E402

SOURCE = ("// @slider glow 0.0..1.0 = 0.25\n"
          "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow); }\n")
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"


class FakeRender:
    def __init__(self, status="ok"):
        self.calls = []
        self.status = status

    def __call__(self, source, out_dir, *, sliders=None, values=None, timeout_s=30.0):
        self.calls.append({"source": source, "values": dict(values or {}), "out_dir": str(out_dir)})
        if self.status != "ok":
            return renderer.BundleResult(self.status, detail="boom")
        path = pathlib.Path(out_dir) / renderer.THUMB_NAME
        path.write_bytes(WEBP)
        (pathlib.Path(out_dir) / "browser.log").write_text("thumb run")
        return renderer.BundleResult("ok", thumb=str(path))


@pytest.fixture
def lib(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    record.ensure_schema(conn)
    yield conn, tmp_path
    conn.close()


def save_version(conn, base, shader_id, version, *, tuned=None, thumb=False, **extra):
    folder = record.version_dir(base, shader_id, version)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shader.frag").write_text(SOURCE)
    (folder / "browser.log").write_text("original render")
    (folder / "frame-t1.png").write_bytes(b"png")
    files = {"frag": str(folder / "shader.frag"), "contact_sheet": None, "snapshot": None}
    if thumb:
        (folder / "thumb.webp").write_bytes(WEBP)
        files["thumb"] = str(folder / "thumb.webp")
    doc = {"id": shader_id, "version": version, "name": "glow", "source": SOURCE,
           "sliders": record.parse_sliders(SOURCE), "tuned_values": tuned or {}, "channels": [],
           "provenance": {"origin": "agent", "licence": None, "parent": None}, "checks": {"status": "ok"},
           "edited_by": "agent", "created_at": f"2026-11-0{version}T00:00:00Z", "files": files}
    doc.update(extra)
    return record.save(conn, doc)


def test_missing_lists_versions_without_a_thumb_on_disk(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1, thumb=True)
    save_version(conn, base, "a-glow", 2)
    save_version(conn, base, "b-glow", 1)
    assert thumbs.missing(conn) == [{"id": "a-glow", "version": 2}, {"id": "b-glow", "version": 1}]


def test_a_thumb_is_rendered_with_the_effective_values_and_stored_beside_the_version(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1, tuned={"glow": 0.8})
    fake = FakeRender()
    assert thumbs.make_thumb(conn, base, "a-glow", 1, render=fake) is None
    folder = record.version_dir(base, "a-glow", 1)
    assert (folder / "thumb.webp").read_bytes() == WEBP
    assert (folder / "browser.log").read_text() == "original render"
    assert fake.calls[0]["values"] == {"glow": 0.8}
    assert record.get_version(conn, "a-glow", 1)["files"]["thumb"] == str(folder / "thumb.webp")
    assert thumbs.data_url(record.thumb_path(record.get_version(conn, "a-glow", 1))).startswith(
        "data:image/webp;base64,")


def test_a_failed_thumb_render_is_reported_not_stored(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1)
    assert thumbs.make_thumb(conn, base, "a-glow", 1, render=FakeRender("timeout")) == {"error": "boom"}
    assert record.thumb_path(record.get_version(conn, "a-glow", 1)) is None


def test_pruning_keeps_thumbs(lib):
    conn, base = lib
    for version in (1, 2, 3):
        save_version(conn, base, "a-glow", version, thumb=True)
    record.prune_versions(base, "a-glow", keep=1)
    old = record.version_dir(base, "a-glow", 1)
    assert not (old / "frame-t1.png").exists() and (old / "thumb.webp").is_file()


def test_backfill_job_fills_every_missing_thumb(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1)
    save_version(conn, base, "a-glow", 2, thumb=True)
    save_version(conn, base, "b-glow", 1)
    factory = lambda: sqlite3.connect(base / "data.db")  # noqa: E731
    job = thumbs.start_backfill(conn, base, factory, background=False, render=FakeRender())
    assert job["state"] == "done" and job["done"] == job["total"] == 2 and job["errors"] == []
    assert thumbs.missing(conn) == []


def test_tuning_refreshes_the_latest_thumb_with_the_new_values(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1, thumb=True)
    record.set_slider_values(conn, "a-glow", {"glow": 0.9})
    fake = FakeRender()
    factory = lambda: sqlite3.connect(base / "data.db")  # noqa: E731
    job = thumbs.start_refresh(conn, base, factory, "a-glow", background=False, render=fake)
    assert job["state"] == "done" and fake.calls[0]["values"] == {"glow": 0.9}


def test_meta_defaults_and_licence_restamping_touch_only_the_latest_version(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1)
    save_version(conn, base, "a-glow", 2, name="glow \u2014 v2")
    meta = record.get_meta(conn, "a-glow")
    assert meta["name"] == "glow \u2014 v2" and meta["licence"] is None and meta["tags"] == []
    record.set_licence(conn, "a-glow", "MIT")
    assert record.get_meta(conn, "a-glow")["licence"] == "MIT"
    assert record.get_version(conn, "a-glow", 2)["provenance"]["licence"] == "MIT"
    assert record.get_version(conn, "a-glow", 1)["provenance"]["licence"] is None
    assert record.display_name("glow \u2014 v2") == "glow - v2"


def test_a_fork_of_an_older_version_records_it_and_keeps_its_thumb_and_licence(lib):
    conn, base = lib
    save_version(conn, base, "a-glow", 1, thumb=True)
    save_version(conn, base, "a-glow", 2, thumb=True)
    record.save_meta(conn, {**record.new_meta("a-glow", "glow"), "licence": "CC-BY-NC-SA-4.0",
                            "licence_inherited": "CC-BY-NC-SA-4.0"})
    forked = record.fork(conn, base, "a-glow", version=1)
    assert forked["provenance"]["parent"] == "a-glow" and forked["provenance"]["parent_version"] == 1
    assert pathlib.Path(forked["files"]["thumb"]).parent == record.version_dir(base, forked["id"], 1)
    assert pathlib.Path(forked["files"]["thumb"]).is_file()
    assert record.get_meta(conn, forked["id"])["licence_inherited"] == "CC-BY-NC-SA-4.0"


def test_records_before_v02_read_as_written_by_the_agent(lib):
    conn, base = lib
    doc = save_version(conn, base, "a-glow", 1, edited_by="human")
    assert record.author_kind(doc) == "agent"
    assert record.author_kind({**doc, "author_kind": "human"}) == "human"
    assert record.author_kind({**doc, "provenance": {"origin": "import"}}) == "import"


def make_flat(base, folder, status, *, frames=True):
    path = base / "shaders" / folder
    path.mkdir(parents=True)
    (path / "shader.frag").write_text(SOURCE)
    (path / "result.json").write_text(json.dumps({"status": status}))
    if frames:
        (path / "frame.png").write_bytes(b"png")
    return path


def test_only_ok_flat_folders_are_adoptable_and_the_others_are_named_with_a_reason(lib):
    conn, base = lib
    make_flat(base, "20261108-014410-673868-warm-palette-sweep", "ok")
    make_flat(base, "20261105-232425-504797-golden-hour-sky", "browser_error", frames=False)
    make_flat(base, "20261105-233018-803204-golden-hour-sky", "compile_error", frames=False)
    save_version(conn, base, "20261108-000000-000000-recorded", 1)
    legacy = {item["folder"]: item for item in record.legacy_folders(conn, base)}
    assert set(legacy) == {"20261108-014410-673868-warm-palette-sweep",
                           "20261105-232425-504797-golden-hour-sky", "20261105-233018-803204-golden-hour-sky"}
    ok = legacy["20261108-014410-673868-warm-palette-sweep"]
    assert ok["adoptable"] and ok["name"] == "warm palette sweep" and ok["created_at"] == "2026-11-08T01:44:10Z"
    assert "browser error" in legacy["20261105-232425-504797-golden-hour-sky"]["reason"]


def test_adopt_rerenders_ok_folders_and_lists_the_rest_without_touching_them(lib):
    conn, base = lib
    folder = make_flat(base, "20261108-014410-673868-warm-palette-sweep", "ok")
    make_flat(base, "20261105-232425-504797-golden-hour-sky", "browser_error", frames=False)
    calls = []

    def render_version(conn_, base_, **kwargs):
        calls.append(kwargs)
        save_version(conn_, base_, kwargs["adopt"]["id"], 1, created_at=kwargs["adopt"]["created_at"])
        return {"success": True, "id": kwargs["adopt"]["id"], "version": 1}

    factory = lambda: sqlite3.connect(base / "data.db")  # noqa: E731
    job = thumbs.start_adopt(conn, base, factory, render_version, background=False)
    assert job["state"] == "done"
    assert job["result"]["adopted"] == [{"folder": folder.name, "id": folder.name}]
    assert [s["folder"] for s in job["result"]["skipped"]] == ["20261105-232425-504797-golden-hour-sky"]
    assert calls[0]["name"] == "warm palette sweep"
    assert (folder / "result.json").is_file() and (folder / "frame.png").is_file()
    assert thumbs.adoptable(conn, base) == []
