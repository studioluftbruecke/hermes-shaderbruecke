"""Two writers on one shader at once: the agent's render, the Lab's save and the jobs. Each version
number is given once, an attempt still rendering is never pruned by another one's success, a Lab
save whose base moved during its render answers `stale_base`, and a job has one worker. The
browser is faked; the threads and the database are real."""

import json
import pathlib
import sqlite3
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import jobs, record, renderer  # noqa: E402

SOURCE = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) "
          "{ fragColor = vec4(fragCoord.x / iResolution.x, 0.5 + 0.5 * sin(iTime), 0.0, 1.0); }\n")


class FakeBrowser:
    """`render_bundle` that writes its frames, then waits: every render in `overlap` is inside the
    browser at the same time, and a source containing `slow` finishes `delay` seconds later."""

    def __init__(self, overlap: int, delay: float = 0.3):
        self.barrier = threading.Barrier(overlap, timeout=10) if overlap > 1 else None
        self.delay = delay

    def __call__(self, source, out_dir, **kwargs):
        frames = []
        for index, t in enumerate(kwargs.get("times", (0.0, 1.0, 2.5))):
            png = pathlib.Path(out_dir) / f"frame-{index}.png"
            png.write_bytes(b"png")
            frames.append({"t": t, "png_path": str(png), "luma_mean": 0.3 + 0.1 * index, "luma_std": 0.2,
                           "nan_fraction": 0.0, "moved": True})
        if self.barrier is not None:
            self.barrier.wait()
        if "slow" in source:
            time.sleep(self.delay)
        return renderer.BundleResult("ok", frames=frames, flash={})


@pytest.fixture
def lib(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", FakeBrowser(1))
    monkeypatch.setattr(PLUGIN.settings_mod, "read_settings",
                        lambda: {"keep_versions": 5, "allow_project_writes": False, "credit_line": True,
                                 "author_name": "", "author_url": ""})
    conn = sqlite3.connect(db)
    first = json.loads(PLUGIN._render(conn, base, source=SOURCE, name="sky", keep_iterating=True))
    assert first["success"] and first["version"] == 1
    conn.close()
    return db, base, first["id"]


def versions(db, shader_id):
    conn = sqlite3.connect(db)
    try:
        return [(v, json.loads(r)) for v, r in conn.execute(
            "SELECT version, record FROM shaders WHERE id = ? ORDER BY version", (shader_id,))]
    finally:
        conn.close()


def run_together(*calls):
    results, errors = [None] * len(calls), []

    def run(index, call):
        try:
            results[index] = call()
        except Exception as exc:  # the assertion below names it
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


def test_two_agent_renders_of_one_shader_get_two_versions(lib, monkeypatch):
    db, base, shader_id = lib
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", FakeBrowser(2))

    def agent(tag):
        return lambda: json.loads(PLUGIN._render(sqlite3.connect(db), base, source=SOURCE + tag, name="sky",
                                                 requested_id=shader_id, keep_iterating=True))

    fast, slow = run_together(agent("// fast\n"), agent("// slow\n"))
    assert fast["success"] and slow["success"], (fast, slow)
    assert sorted([fast["version"], slow["version"]]) == [2, 3]
    stored = dict(versions(db, shader_id))
    assert stored[fast["version"]]["source"].endswith("// fast\n")
    assert stored[slow["version"]]["source"].endswith("// slow\n")
    for answer in (fast, slow):
        assert pathlib.Path(answer["frag_path"]).read_text() == stored[answer["version"]]["source"]


def test_a_lab_save_whose_base_moved_during_its_render_is_stale(lib, monkeypatch):
    db, base, shader_id = lib
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", FakeBrowser(2))

    def agent():
        return json.loads(PLUGIN._render(sqlite3.connect(db), base, source=SOURCE + "// agent\n", name="sky",
                                         requested_id=shader_id, keep_iterating=True))

    def human():
        return PLUGIN.save_human_version(sqlite3.connect(db), base, shader_id, SOURCE + "// slow human\n", 1)

    by_agent, by_human = run_together(agent, human)
    assert by_agent["success"] and by_agent["version"] == 2
    assert by_human.get("refused") == 409 and by_human["error"] == "stale_base" and by_human["latest"] == 2
    stored = versions(db, shader_id)
    assert [v for v, _ in stored] == [1, 2]
    assert stored[1][1]["author_kind"] == "agent"


class DeletingBrowser(FakeBrowser):
    """A render during which the user deletes the shader in Desktop."""

    def __init__(self, db, base, shader_id):
        super().__init__(1)
        self.db, self.base, self.shader_id = db, base, shader_id

    def __call__(self, source, out_dir, **kwargs):
        answer = super().__call__(source, out_dir, **kwargs)
        conn = sqlite3.connect(self.db)
        record.add_human_notice(conn, self.base, self.shader_id, 1)
        record.delete(conn, self.base, self.shader_id)
        conn.close()
        return answer


def test_an_agent_render_of_a_shader_deleted_meanwhile_stores_nothing(lib, monkeypatch):
    db, base, shader_id = lib
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", DeletingBrowser(db, base, shader_id))
    answer = json.loads(PLUGIN._render(sqlite3.connect(db), base, source=SOURCE + "// agent\n", name="sky",
                                       requested_id=shader_id, keep_iterating=True))
    assert answer["success"] is False and answer["status"] == "not_found", answer
    assert versions(db, shader_id) == []
    assert not record.shader_dir(base, shader_id).exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM human_notices").fetchone()[0] == 0
    conn.close()


def test_a_lab_save_of_a_shader_deleted_meanwhile_says_so(lib, monkeypatch):
    db, base, shader_id = lib
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", DeletingBrowser(db, base, shader_id))
    monkeypatch.setattr(PLUGIN.renderer, "free_slots", lambda directory: 2)
    answer = PLUGIN.save_human_version(sqlite3.connect(db), base, shader_id, SOURCE + "// human\n", 1)
    assert answer == {"refused": 404, "error": "not_found", "message": "This shader was deleted."}
    assert versions(db, shader_id) == []
    again = PLUGIN.save_human_version(sqlite3.connect(db), base, shader_id, SOURCE + "// human\n", 1)
    assert again["refused"] == 404 and again["message"] == "This shader was deleted."


def test_an_attempt_still_rendering_survives_another_attempts_success(lib, monkeypatch):
    db, base, shader_id = lib
    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", FakeBrowser(2))

    def slow_render():
        return json.loads(PLUGIN._render(sqlite3.connect(db), base, source=SOURCE + "// slow\n", name="sky",
                                         requested_id=shader_id, keep_iterating=True))

    def ok():
        return json.loads(PLUGIN._render(sqlite3.connect(db), base, source=SOURCE + "// quick\n", name="sky",
                                         requested_id=shader_id, keep_iterating=True))

    slow, quick = run_together(slow_render, ok)
    assert slow["success"] and quick["success"]


def test_a_worker_that_lost_its_job_stops_and_counts_nothing(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    job = jobs.create(conn, "thumbs", ["a", "b", "c"], now=1000.0)
    assert jobs.claim(conn, job["id"], "first", now=1000.0)
    assert jobs.claim(conn, job["id"], "second", now=1000.0 + jobs.STALE_S + 1)
    assert jobs.step(conn, job["id"], "first", now=1000.0 + jobs.STALE_S + 2) is False
    assert jobs.get(conn, job["id"])["done"] == 0
    assert jobs.step(conn, job["id"], "second", now=1000.0 + jobs.STALE_S + 2) is True
    assert jobs.get(conn, job["id"])["done"] == 1


def test_a_job_outlives_the_longest_wait_for_a_renderer():
    assert jobs.STALE_S > renderer.SLOT_WAIT_S + 30.0


def test_concurrent_starts_of_one_kind_make_one_job(tmp_path):
    path = tmp_path / "data.db"
    setup = sqlite3.connect(path)
    jobs.ensure_schema(setup)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.close()
    gate = threading.Barrier(8, timeout=10)

    def start():
        conn = sqlite3.connect(path, timeout=10)
        gate.wait()
        try:
            return jobs.start_or_resume(conn, "thumbs", lambda: (time.sleep(0.05), ["a"])[1])[0]["id"]
        finally:
            conn.close()

    ids = run_together(*[start] * 8)
    assert len(set(ids)) == 1, ids


def test_finished_jobs_older_than_a_week_are_pruned(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    old = jobs.create(conn, "thumb_refresh", ["a"], now=1000.0)
    assert jobs.claim(conn, old["id"], "w", now=1000.0)
    jobs.finish(conn, old["id"], "w")
    conn.execute("UPDATE jobs SET heartbeat_at = 1000.0 WHERE id = ?", (old["id"],))
    conn.commit()
    jobs.create(conn, "thumb_refresh", ["b"])
    assert jobs.get(conn, old["id"]) is None
