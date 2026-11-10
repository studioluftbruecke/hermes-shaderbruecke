"""Library thumbnails (F36) and adopting the flat folders of the first builds (D14).

Every version gets a 320x180 `thumb.webp` drawn from its t = 1 s frame with the effective slider
values. A new render writes it in the same browser session (`renderer.render_bundle`). Older
versions get it from the backfill job, and a slider change refreshes the latest version's thumb as
a job. Thumbs survive pruning (only `.png` and `.log` are pruned).

The work functions take the connection and the data dir as arguments (the `record.py` seam), so
tests run them against a temp dir; the browser work goes through `renderer`, which tests replace.
"""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

from . import jobs, record, renderer


def version_folder(base: Path, doc: dict) -> Path:
    frag = (doc.get("files") or {}).get("frag")
    if frag and Path(frag).parent.is_dir():
        return Path(frag).parent
    return record.version_dir(base, doc["id"], doc["version"])


def data_url(path: Path | None) -> str | None:
    """`data:image/webp;base64,...` for a thumb on disk (the route payload; P15/P16: 2 to 20 KB)."""
    if path is None:
        return None
    try:
        return "data:image/webp;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError:
        return None


def missing(conn: sqlite3.Connection) -> list[dict]:
    """`{id, version}` of every version without a thumb on disk, newest shaders first."""
    out = []
    for doc in record.all_versions(conn):
        if record.thumb_path(doc) is None:
            out.append({"id": doc["id"], "version": int(doc["version"]),
                        "created_at": doc.get("created_at") or ""})
    out.sort(key=lambda item: (item["created_at"], item["version"]), reverse=True)
    return [{"id": item["id"], "version": item["version"]} for item in out]


def make_thumb(conn: sqlite3.Connection, base: Path, shader_id: str, version: int, *,
               values: dict | None = None,
               render: Callable[..., renderer.BundleResult] | None = None) -> dict | None:
    """Render one version's thumb with its effective values (or `values`) and store it.

    The browser runs in a scratch folder, so the version keeps its own `browser.log`; only
    `thumb.webp` is moved in. Returns None on success or `{"error": reason}`.
    """
    doc = record.get_version(conn, shader_id, version)
    if doc is None:
        return {"error": "the version no longer exists"}
    folder = version_folder(base, doc)
    if not folder.is_dir():
        return {"error": "the version folder is missing"}
    render = render or renderer.render_thumb
    effective = record.effective_values(doc) if values is None else values
    with tempfile.TemporaryDirectory(prefix="sb-thumb-", dir=renderer.short_temp_base()) as scratch:
        with renderer.chromium_slots(Path(base) / "locks"):
            result = render(doc.get("source") or "", Path(scratch), sliders=doc.get("sliders") or [],
                            values=effective)
        if result.status != "ok" or not result.thumb:
            return {"error": result.detail or f"render ended in {result.status}"}
        target = folder / renderer.THUMB_NAME
        shutil.move(result.thumb, target)
    with record.shader_lock(base, shader_id):
        doc = record.get_version(conn, shader_id, version) or doc
        files = dict(doc.get("files") or {})
        if files.get("thumb") != str(target):
            files["thumb"] = str(target)
            doc["files"] = files
            record.save(conn, doc)
    return None


def adopt_one(conn: sqlite3.Connection, base: Path, legacy: dict,
              render_version: Callable[..., dict]) -> dict:
    """Re-render one flat folder into `<id>/v1` through the normal render path (`render_version`
    is `__init__._render` with the adopt keyword). Nothing in the flat folder is changed."""
    frag = Path(base) / "shaders" / legacy["folder"] / "shader.frag"
    try:
        source = frag.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"shader.frag unreadable: {exc}"}
    payload = render_version(conn, base, source=source, name=legacy["name"],
                             adopt={"id": legacy["folder"], "created_at": legacy["created_at"]})
    if not payload.get("success"):
        return {"error": f"the re-render failed: {payload.get('error')}"}
    return {"result": {"adopted": {"folder": legacy["folder"], "id": payload["id"]}}}


def adoptable(conn: sqlite3.Connection, base: Path) -> list[dict]:
    return [item for item in record.legacy_folders(conn, base) if item["adoptable"]]


def skipped(conn: sqlite3.Connection, base: Path) -> list[dict]:
    return [{"folder": item["folder"], "reason": item["reason"]}
            for item in record.legacy_folders(conn, base) if not item["adoptable"]]


# ---------------------------------------------------------------- starting jobs

def start_backfill(conn: sqlite3.Connection, base: Path, conn_factory: Callable[[], sqlite3.Connection],
                   *, background: bool = True, render=None) -> dict:
    """Start (or resume, or return the running) thumb backfill job."""
    job, ours = jobs.start_or_resume(conn, "thumbs", lambda: missing(conn))

    def handle(item):
        with contextlib.closing(conn_factory()) as worker:
            return make_thumb(worker, base, item["id"], int(item["version"]), render=render)

    return _run(conn, conn_factory, job, ours, handle, background)


def start_refresh(conn: sqlite3.Connection, base: Path, conn_factory: Callable[[], sqlite3.Connection],
                  shader_id: str, *, background: bool = True, render=None) -> dict:
    """After slider tuning: re-render the latest version's thumb with the new effective values."""
    newest = record.latest(conn, shader_id)
    if newest is None:
        return {}
    job = jobs.create(conn, "thumb_refresh", [{"id": shader_id, "version": int(newest["version"])}])

    def handle(item):
        with contextlib.closing(conn_factory()) as worker:
            return make_thumb(worker, base, item["id"], int(item["version"]), render=render)

    return _run(conn, conn_factory, job, True, handle, background)


def start_adopt(conn: sqlite3.Connection, base: Path, conn_factory: Callable[[], sqlite3.Connection],
                render_version: Callable[..., dict], *, background: bool = True) -> dict:
    """Adopt every `ok` flat folder (D14: only on the user's click or command, never on its own);
    the result lists the adopted ids, the job's `skipped` the folders that were never adoptable."""
    job, ours = jobs.start_or_resume(conn, "adopt", lambda: adoptable(conn, base))

    def handle(item):
        with contextlib.closing(conn_factory()) as worker:
            return adopt_one(worker, base, item, render_version)

    public = _run(conn, conn_factory, job, ours, handle, background)
    if public:
        result = dict(public.get("result") or {})
        result["skipped"] = skipped(conn, base) + [
            {"folder": error["item"], "reason": error["reason"]} for error in public.get("errors") or []]
        public["result"] = result
    return public


def _run(conn, conn_factory, job: dict, ours: bool, handle, background: bool) -> dict:
    if ours and job["items"][job["done"]:]:
        if background:
            jobs.work_in_thread(conn_factory, job["id"], handle)
        else:
            return jobs.work(conn_factory, job["id"], handle, owner=f"cli-{os.getpid()}") or {}
    elif ours:
        owner = f"empty-{os.getpid()}"
        if jobs.claim(conn, job["id"], owner):
            jobs.finish(conn, job["id"], owner)
    return jobs.public(jobs.get(conn, job["id"])) or {}
