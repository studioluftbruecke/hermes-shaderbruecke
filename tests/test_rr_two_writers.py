"""Writers that replace a whole version row (slider values, reset, licence, the thumb job) never
bring back a deleted shader and never lose a licence stamp to a version that lands meanwhile.

Each case forces the interleaving: the writer pauses right after it read the version, the other
writer runs, then the first one continues. Real threads, one sqlite file, own connections.
"""

import contextlib
import json
import pathlib
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from sb_fakes import PLUGIN  # noqa: E402
from shaderbruecke import record, renderer, thumbs  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402


@pytest.fixture
def profile(tmp_path, monkeypatch):
    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    return db, base, sb_fakes.new_shader(db, base)


def rows(db, shader_id):
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return (conn.execute("SELECT COUNT(*) FROM shaders WHERE id = ?", (shader_id,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM shader_meta WHERE id = ?", (shader_id,)).fetchone()[0])


def pause_reads_in(monkeypatch, thread_name):
    """`record.latest` and `record.get_version` pause in `thread_name` until released."""
    reached, release = threading.Event(), threading.Event()
    for name in ("latest", "get_version"):
        real = getattr(record, name)

        def paused(*args, _real=real, **kwargs):
            doc = _real(*args, **kwargs)
            if threading.current_thread().name == thread_name and not release.is_set():
                reached.set()
                release.wait(5)
            return doc
        monkeypatch.setattr(record, name, paused)
    return reached, release


def run(name, target):
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def race_against_delete(db, base, shader_id, monkeypatch, write):
    reached, release = pause_reads_in(monkeypatch, "writer")
    errors = []

    def writer():
        with contextlib.closing(sqlite3.connect(db)) as conn:
            try:
                write(conn)
            except Exception as exc:  # noqa: BLE001 - the assertion below names it
                errors.append(exc)

    first = run("writer", writer)
    assert reached.wait(5)
    deleter = run("deleter", lambda: record.delete(sqlite3.connect(db), base, shader_id))
    deleter.join(0.3)                 # the delete either finishes now or waits for the writer's lock
    release.set()
    first.join(5)
    deleter.join(5)
    assert not errors, errors
    assert rows(db, shader_id) == (0, 0), "the deleted shader came back"


def test_slider_values_racing_a_delete_do_not_bring_the_shader_back(profile, monkeypatch):
    db, base, shader_id = profile
    race_against_delete(db, base, shader_id, monkeypatch,
                        lambda conn: library_api.set_values(conn, base, shader_id, {"sunHeight": 0.7}))


def test_a_reset_racing_a_delete_does_not_bring_the_shader_back(profile, monkeypatch):
    db, base, shader_id = profile
    race_against_delete(db, base, shader_id, monkeypatch,
                        lambda conn: library_api.reset_values(conn, base, shader_id))


def test_a_licence_change_racing_a_delete_does_not_bring_the_shader_back(profile, monkeypatch):
    db, base, shader_id = profile
    race_against_delete(db, base, shader_id, monkeypatch,
                        lambda conn: record.set_licence(conn, base, shader_id, "MIT"))


def test_a_licence_change_racing_a_new_version_stamps_that_version(profile, monkeypatch):
    db, base, shader_id = profile
    reached, release = pause_reads_in(monkeypatch, "licence")

    def licence():
        with contextlib.closing(sqlite3.connect(db)) as conn:
            record.set_licence(conn, base, shader_id, "MIT")

    def render():
        with contextlib.closing(sqlite3.connect(db)) as conn:
            answer = json.loads(PLUGIN._render(conn, base, source=sb_fakes.SKY, name="sky",
                                               requested_id=shader_id, keep_iterating=True))
            assert answer["success"], answer

    first = run("licence", licence)
    assert reached.wait(5)
    second = run("render", render)
    second.join(0.5)
    release.set()
    first.join(5)
    second.join(5)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        newest = record.latest(conn, shader_id)
        assert newest["version"] == 2
        assert newest["provenance"]["licence"] == "MIT"
        assert record.get_meta(conn, shader_id)["licence"] == "MIT"


def test_the_thumb_job_does_not_resave_a_version_deleted_during_its_render(profile, monkeypatch):
    db, base, shader_id = profile

    def render_then_delete(source, out_dir, **kwargs):
        thumb = pathlib.Path(out_dir) / renderer.THUMB_NAME
        thumb.write_bytes(b"RIFF fake")
        with contextlib.closing(sqlite3.connect(db)) as other:
            record.delete(other, base, shader_id)
        return renderer.BundleResult("ok", thumb=str(thumb))

    with contextlib.closing(sqlite3.connect(db)) as conn:
        answer = thumbs.make_thumb(conn, base, shader_id, 1, render=render_then_delete)
    assert answer and "no longer exists" in answer["error"]
    assert rows(db, shader_id) == (0, 0)


def test_writes_on_a_live_shader_still_land(profile):
    db, base, shader_id = profile
    with contextlib.closing(sqlite3.connect(db)) as conn:
        assert library_api.set_values(conn, base, shader_id, {"sunHeight": 0.7})["tuned_values"] == {"sunHeight": 0.7}
        assert library_api.reset_values(conn, base, shader_id)["tuned_values"] == {}
        record.set_licence(conn, base, shader_id, "MIT")
        assert record.latest(conn, shader_id)["provenance"]["licence"] == "MIT"
        assert library_api.set_values(conn, base, "nope", {"sunHeight": 0.7})["status"] == 404
