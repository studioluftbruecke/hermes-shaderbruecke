"""Background jobs: thumb backfill, thumb refresh after tuning, adopt.

A job is a row in `data.db` (`jobs`), worked by a thread in the process that started it (the
plugin host for a Desktop click, the CLI for `hermes shaderbruecke thumbs|adopt`). The thread
writes a heartbeat after every item; a job whose heartbeat is older than `STALE_S` reads as stale,
and the next start of that kind resumes it from its first unfinished item instead of starting a
second one. Items are fixed when the job is created, so a resume works the same list.

Every item takes one Chromium slot through the renderer and releases it before the next item, so
the agent's renders interleave with a long job.
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
import time
import uuid
from typing import Callable

# One item can wait `renderer.SLOT_WAIT_S` (120 s) for a Chromium slot and then render for up to
# 30 s, so a heartbeat per item needs a longer silence before another worker may take the job.
STALE_S = 240.0
KEEP_FINISHED_S = 7 * 24 * 3600.0
KINDS = ("thumbs", "adopt", "thumb_refresh")
ACTIVE = ("queued", "running")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT, state TEXT, "
                 "items TEXT, done INTEGER, errors TEXT, result TEXT, started_at REAL, "
                 "heartbeat_at REAL, owner TEXT)")
    conn.commit()


def _iso(seconds: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds)) if seconds else None


def _row(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT id, kind, state, items, done, errors, result, started_at, heartbeat_at, owner "
                       "FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "kind", "state", "items", "done", "errors", "result", "started_at", "heartbeat_at", "owner")
    job = dict(zip(keys, row))
    job["items"] = json.loads(job["items"] or "[]")
    job["errors"] = json.loads(job["errors"] or "[]")
    job["result"] = json.loads(job["result"]) if job["result"] else None
    return job


def is_stale(job: dict, now: float | None = None) -> bool:
    return job["state"] in ACTIVE and ((now or time.time()) - (job["heartbeat_at"] or 0)) > STALE_S


def public(job: dict | None, now: float | None = None) -> dict | None:
    """The contract's `Job` shape."""
    if job is None:
        return None
    state = "stale" if is_stale(job, now) else job["state"]
    return {"id": job["id"], "kind": job["kind"], "state": state, "done": job["done"],
            "total": len(job["items"]), "errors": job["errors"],
            "startedAt": _iso(job["started_at"]), "heartbeatAt": _iso(job["heartbeat_at"]),
            "result": job["result"]}


def get(conn: sqlite3.Connection, job_id: str) -> dict | None:
    ensure_schema(conn)
    return _row(conn, job_id)


def active(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    ensure_schema(conn)
    return _active(conn, kind)


def _active(conn: sqlite3.Connection, kind: str | None) -> list[dict]:
    query = "SELECT id FROM jobs WHERE state IN ('queued', 'running')"
    params: tuple = ()
    if kind:
        query += " AND kind = ?"
        params = (kind,)
    return [job for (job_id,) in conn.execute(query + " ORDER BY started_at", params)
            if (job := _row(conn, job_id)) is not None]


def _insert(conn: sqlite3.Connection, kind: str, items: list, now: float | None) -> str:
    """The INSERT of a new job, inside the caller's transaction; finished jobs older than a week
    are dropped on the way."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind}")
    started = now or time.time()
    job_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    conn.execute("DELETE FROM jobs WHERE state NOT IN ('queued', 'running') AND heartbeat_at < ?",
                 (started - KEEP_FINISHED_S,))
    conn.execute("INSERT INTO jobs (id, kind, state, items, done, errors, result, started_at, heartbeat_at, owner) "
                 "VALUES (?, ?, 'queued', ?, 0, '[]', NULL, ?, ?, NULL)",
                 (job_id, kind, json.dumps(items), started, started))
    return job_id


def create(conn: sqlite3.Connection, kind: str, items: list, now: float | None = None) -> dict:
    ensure_schema(conn)
    job_id = _insert(conn, kind, items, now)
    conn.commit()
    return _row(conn, job_id)


def claim(conn: sqlite3.Connection, job_id: str, owner: str, now: float | None = None) -> bool:
    """Take a queued or stale job for `owner`. False when another live worker holds it."""
    stamp = now or time.time()
    cursor = conn.execute(
        "UPDATE jobs SET state = 'running', owner = ?, heartbeat_at = ? WHERE id = ? AND "
        "(state = 'queued' OR (state = 'running' AND (? - heartbeat_at) > ?))",
        (owner, stamp, job_id, stamp, STALE_S))
    conn.commit()
    return cursor.rowcount == 1


def step(conn: sqlite3.Connection, job_id: str, owner: str, *, error: dict | None = None,
         result_add: dict | None = None, now: float | None = None) -> bool:
    """One item finished: advance `done`, record an error or a result entry, beat the heart.
    False when `owner` no longer holds the job (another worker took it over as stale)."""
    job = _row(conn, job_id)
    if job is None or job["owner"] != owner:
        return False
    errors = job["errors"] + ([error] if error else [])
    result = job["result"] or {}
    for key, value in (result_add or {}).items():
        result.setdefault(key, []).append(value)
    cursor = conn.execute("UPDATE jobs SET done = done + 1, errors = ?, result = ?, heartbeat_at = ? "
                          "WHERE id = ? AND owner = ?",
                          (json.dumps(errors), json.dumps(result) if result else None, now or time.time(),
                           job_id, owner))
    conn.commit()
    return cursor.rowcount == 1


def finish(conn: sqlite3.Connection, job_id: str, owner: str, state: str = "done") -> None:
    conn.execute("UPDATE jobs SET state = ?, heartbeat_at = ? WHERE id = ? AND owner = ?",
                 (state, time.time(), job_id, owner))
    conn.commit()


def start_or_resume(conn: sqlite3.Connection, kind: str, items_fn: Callable[[], list],
                    now: float | None = None) -> tuple[dict, bool]:
    """The job of `kind` to work: a live one is returned as is (`False`: someone works it), a
    stale one is resumed, otherwise a new one is created from `items_fn()`. `True` means the caller
    should work it."""
    current = active(conn, kind)
    if current:
        return current[0], is_stale(current[0], now)
    items = items_fn()
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")           # check again and create, one caller at a time
    try:
        current = _active(conn, kind)
        if current:
            conn.commit()
            return current[0], is_stale(current[0], now)
        job_id = _insert(conn, kind, items, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return _row(conn, job_id), True


def work(conn_factory: Callable[[], sqlite3.Connection], job_id: str,
         handle: Callable[[object], dict | None], *, owner: str | None = None) -> dict | None:
    """Work a claimed job to the end in this thread. `handle(item)` returns None on success, a
    dict `{"error": reason}` to record a skipped item, or `{"result": {key: value}}`."""
    owner = owner or uuid.uuid4().hex
    conn = conn_factory()
    try:
        ensure_schema(conn)
        if not claim(conn, job_id, owner):
            return public(get(conn, job_id))
        job = get(conn, job_id)
        for item in job["items"][job["done"]:]:
            try:
                outcome = handle(item) or {}
            except Exception as exc:  # one bad item must not stop the job
                outcome = {"error": f"{type(exc).__name__}: {exc}"}
            error = {"item": _label(item), "reason": outcome["error"]} if outcome.get("error") else None
            if not step(conn, job_id, owner, error=error, result_add=outcome.get("result")):
                return public(get(conn, job_id))
        finish(conn, job_id, owner)
        return public(get(conn, job_id))
    finally:
        conn.close()


def work_in_thread(conn_factory: Callable[[], sqlite3.Connection], job_id: str,
                   handle: Callable[[object], dict | None]) -> threading.Thread:
    """A daemon thread that works the job. It runs in a copy of the caller's context: in the
    gateway the profile's home is a context variable, and a bare thread would fall back to the
    process's default home."""
    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(work, conn_factory, job_id, handle),
                              name=f"shaderbruecke-{job_id}", daemon=True)
    thread.start()
    return thread


def _label(item) -> str:
    if isinstance(item, dict):
        return str(item.get("folder") or (f"{item.get('id')}@v{item.get('version')}" if item.get("id") else item))
    return str(item)
