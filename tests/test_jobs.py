"""Background jobs: one live job per kind, a stale job resumes from its first unfinished item,
an item that fails is recorded and the job goes on."""

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import jobs  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    jobs.ensure_schema(conn)
    yield conn, (lambda: sqlite3.connect(path))
    conn.close()


def test_a_job_works_every_item_and_records_failures(db):
    conn, factory = db
    job = jobs.create(conn, "thumbs", ["a", "b", "c"])
    seen = []

    def handle(item):
        seen.append(item)
        return {"error": "no frame"} if item == "b" else None

    public = jobs.work(factory, job["id"], handle)
    assert seen == ["a", "b", "c"]
    assert public["state"] == "done" and public["done"] == 3 and public["total"] == 3
    assert public["errors"] == [{"item": "b", "reason": "no frame"}]


def test_an_exception_in_one_item_does_not_stop_the_job(db):
    conn, factory = db
    job = jobs.create(conn, "thumbs", ["a", "b"])

    def handle(item):
        if item == "a":
            raise RuntimeError("browser closed")

    public = jobs.work(factory, job["id"], handle)
    assert public["done"] == 2 and public["errors"][0]["reason"] == "RuntimeError: browser closed"


def test_a_live_job_is_returned_instead_of_a_second_one(db):
    conn, _factory = db
    first, ours = jobs.start_or_resume(conn, "thumbs", lambda: ["a"], now=1000.0)
    assert ours and jobs.claim(conn, first["id"], "worker-1", now=1000.0)
    again, ours_again = jobs.start_or_resume(conn, "thumbs", lambda: ["x"], now=1030.0)
    assert again["id"] == first["id"] and ours_again is False
    assert jobs.claim(conn, first["id"], "worker-2", now=1030.0) is False


def test_a_killed_job_reads_stale_and_resumes_from_its_first_unfinished_item(db):
    conn, factory = db
    job, _ = jobs.start_or_resume(conn, "adopt", lambda: ["a", "b", "c"], now=1000.0)
    assert jobs.claim(conn, job["id"], "killed-worker", now=1000.0)
    jobs.step(conn, job["id"], "killed-worker", now=1001.0)          # "a" done, then the process died
    assert jobs.public(jobs.get(conn, job["id"]), now=1030.0)["state"] == "running"
    assert jobs.public(jobs.get(conn, job["id"]), now=1002.0 + jobs.STALE_S)["state"] == "stale"
    resumed, ours = jobs.start_or_resume(conn, "adopt", lambda: ["never"], now=1002.0 + jobs.STALE_S)
    assert resumed["id"] == job["id"] and ours
    seen = []
    public = jobs.work(factory, job["id"], lambda item: seen.append(item))
    assert seen == ["b", "c"] and public["done"] == 3 and public["state"] == "done"


def test_unknown_kinds_are_refused(db):
    conn, _factory = db
    with pytest.raises(ValueError):
        jobs.create(conn, "delete_everything", [])
