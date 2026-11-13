"""Slider saves that arrive while a thumbnail refresh renders share one job: the running render
finishes, then exactly one more render draws the latest values. Each save used to start its own
job, thread and Chromium child."""

import contextlib
import pathlib
import sqlite3
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from shaderbruecke import jobs, record, renderer, thumbs  # noqa: E402
from test_thumbs import save_version  # noqa: E402


class GatedRender:
    """The first render waits until released; every render records the values it drew."""

    def __init__(self):
        self.values, self.release, self.first = [], threading.Event(), threading.Event()

    def __call__(self, source, out_dir, *, sliders=None, values=None, timeout_s=30.0):
        self.values.append(dict(values or {}))
        if len(self.values) == 1:
            self.first.set()
            self.release.wait(5)
        path = pathlib.Path(out_dir) / renderer.THUMB_NAME
        path.write_bytes(b"RIFF fake")
        return renderer.BundleResult("ok", thumb=str(path))


def test_saves_during_a_running_refresh_end_in_one_more_render_with_the_last_values(tmp_path):
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    record.ensure_schema(conn)
    save_version(conn, tmp_path, "a-glow", 1, thumb=True)
    factory = lambda: sqlite3.connect(db)  # noqa: E731
    render = GatedRender()

    first = thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render)
    assert render.first.wait(5)
    for value in (0.2, 0.4, 0.6, 0.8):
        record.set_slider_values(conn, tmp_path, "a-glow", {"glow": value})
        again = thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render)
        assert again["id"] == first["id"]
    render.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (jobs.get(conn, first["id"]) or {}).get("state") != "done":
        time.sleep(0.05)
    with contextlib.closing(sqlite3.connect(db)) as check:
        kinds = [row[0] for row in check.execute("SELECT kind FROM jobs")]
    assert kinds == ["thumb_refresh"]
    assert len(render.values) == 2 and render.values[-1] == {"glow": 0.8}
    # Once that job is done, the next save starts a fresh refresh.
    later = thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render, background=False)
    assert later["id"] != first["id"] and len(render.values) == 3
    conn.close()


def _wait_done(conn, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (jobs.get(conn, job_id) or {}).get("state") != "done":
        time.sleep(0.05)
    return jobs.get(conn, job_id)


def test_a_refresh_started_beside_one_that_reads_as_stale_keeps_working(tmp_path, monkeypatch):
    # A long tuning session renders again and again inside one item, with no heartbeat, so the
    # running job can read as stale and the next save starts a second job beside it.
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    record.ensure_schema(conn)
    save_version(conn, tmp_path, "a-glow", 1, thumb=True)
    factory = lambda: sqlite3.connect(db)  # noqa: E731
    first_render, second_render = GatedRender(), GatedRender()
    calls = []

    def render(source, out_dir, **kwargs):
        calls.append(kwargs.get("values"))
        return (first_render if len(calls) == 1 else second_render)(source, out_dir, **kwargs)

    first = thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render)
    assert first_render.first.wait(5)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "is_stale", lambda job, now=None: True)
        second = thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render)
    assert second["id"] != first["id"] and second_render.first.wait(5)
    first_render.release.set()
    assert _wait_done(conn, first["id"])["errors"] == []
    # A save while the second job still renders joins it, and it draws the latest values.
    record.set_slider_values(conn, tmp_path, "a-glow", {"glow": 0.7})
    assert thumbs.start_refresh(conn, tmp_path, factory, "a-glow", render=render)["id"] == second["id"]
    second_render.release.set()
    assert _wait_done(conn, second["id"])["errors"] == []
    assert len(calls) == 3 and calls[-1] == {"glow": 0.7}
    conn.close()
