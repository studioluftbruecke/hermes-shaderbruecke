"""The plugin API's logic, free of FastAPI and Hermes so a test can call it directly.

`plugin_api.py` is the thin FastAPI layer; everything here takes a `sqlite3.Connection` and a base
`Path` (the same seam as `record.py`). An error is a dict with `error` and the HTTP `status` the
route answers with.

The export and settings modules are imported when a call needs them, through `_exporting()` and
`_settings()`, so a test can replace them and a broken one fails the call, not the import.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

from .. import record, thumbs
from ..exporters import handoff, registry
from ..exporters.header import pascal


def _exporting():
    return importlib.import_module("..exporting", __package__)


def _settings() -> dict:
    return importlib.import_module("..settings", __package__).read_settings()


def _bad_id(shader_id: str) -> dict | None:
    if not record.valid_id(shader_id):
        return {"error": f"invalid shader id {shader_id!r}", "status": 400}
    return None


def _document(conn: sqlite3.Connection, shader_id: str, version: int | None) -> dict | None:
    """The version as the exporters see it (`exporting.export_view`): file names and headers use
    the shader's current name."""
    document = (record.get_version(conn, shader_id, int(version)) if version is not None
                else record.latest(conn, shader_id))
    if document is None:
        return None
    export_view = importlib.import_module("..exporting", __package__).export_view
    return export_view(document, record.get_meta(conn, shader_id), _settings())


def _tuned(document: dict) -> dict:
    tuned = document.get("tuned_values")
    if tuned is None:  # rows written before v0.1.1 kept the human's values here
        tuned = document.get("slider_values") or {}
    return dict(tuned)


def _proof(base: Path, shader_id: str, version: int, target: str) -> dict | None:
    stored = _exporting().read_proof(base, shader_id, version, target)
    if not stored:
        return None
    return {"verdict": stored.get("verdict") or "not_proven", "why": stored.get("why") or ""}


def targets_for(conn: sqlite3.Connection, base: Path, shader_id: str,
                version: int | None = None) -> dict:
    """The export registry as JSON, what this version has on disk, and each target's stored proof.

    The payload also names the shader, its sliders and the human's tuned values, so the Desktop
    card can draw itself from this one call.
    """
    bad = _bad_id(shader_id)
    if bad:
        return bad
    document = _document(conn, shader_id, version)
    if document is None:
        return {"error": f"no shader {shader_id}", "status": 404}
    number = document["version"]
    directory = record.version_dir(base, shader_id, number) / "exports"
    rows = []
    for target_id, row in registry.EXPORT_TARGETS.items():
        main = row.main_file.format(name=pascal(document.get("name")))
        exported = (directory / target_id / main).is_file()
        rows.append({"id": target_id, "label": row.label, "main_file": main,
                     "proof_method": row.proof, "status": row.status, "exported": exported,
                     "proof": _proof(base, shader_id, number, target_id) if exported else None})
    return {"id": shader_id, "version": number, "name": document.get("name"),
            "sliders": document.get("sliders") or [], "tuned_values": _tuned(document),
            "effective_values": record.effective_values(document), "targets": rows}


def export_text(conn: sqlite3.Connection, base: Path, shader_id: str, target: str,
                version: int | None = None) -> dict:
    """The text of a target's main file, from the export folder (the Desktop copies it).

    The agent handoff is generated on request and honours the `credit_line` setting.
    """
    bad = _bad_id(shader_id)
    if bad:
        return bad
    row = registry.EXPORT_TARGETS.get(target)
    if row is None:
        return {"error": f"unknown target {target}", "status": 404}
    document = _document(conn, shader_id, version)
    if document is None:
        return {"error": f"no shader {shader_id}", "status": 404}
    if target == "agent":
        main_file = row.main_file.format(name=pascal(document.get("name")))
        exported = record.version_dir(base, shader_id, document["version"]) / "exports" / "agent" / main_file
        if exported.is_file():
            # The exported handoff carries the acceptance grid its proof measured.
            text = exported.read_text(encoding="utf-8")
        else:
            text = handoff.handoff(document, credit=bool(_settings().get("credit_line", True)))
        return {"id": shader_id, "version": document["version"], "target": target,
                "main_file": main_file, "text": text, "measured": exported.is_file()}
    path = (record.version_dir(base, shader_id, document["version"]) / "exports" / target
            / row.main_file.format(name=pascal(document.get("name"))))
    if not path.is_file():
        return {"error": f"{target} has not been exported yet", "needs_export": True, "status": 409,
                "id": shader_id, "version": document["version"]}
    return {"id": shader_id, "version": document["version"], "target": target,
            "main_file": path.name, "text": path.read_text(encoding="utf-8")}


def export_now(conn: sqlite3.Connection, base: Path, shader_id: str, target: str,
               version: int | None = None) -> dict:
    """Export and prove one target from the card, with the same exporter and proof as the tool.

    No `dest_dir`: the files land in the plugin's own data folder and the card reads them back
    through `export_text`. The payload is `shader_export`'s, verdict included.
    """
    bad = _bad_id(shader_id)
    if bad:
        return bad
    if target not in registry.EXPORT_TARGETS:
        return {"error": f"unknown target {target}", "status": 404}
    if _document(conn, shader_id, version) is None:
        return {"error": f"no shader {shader_id}", "status": 404}
    return _exporting().export_shader(conn, base, shader_id, target, version=version,
                                      settings=_settings())


def set_values(conn: sqlite3.Connection, base: Path, shader_id: str, values: dict,
               version: int | None = None) -> dict:
    """Store the human's slider values (tuned values) on one version, default the latest; exports
    of that version use them from now on."""
    bad = _bad_id(shader_id)
    if bad:
        return bad
    document = record.set_slider_values(conn, base, shader_id, values, version)
    if document is None:
        return _no_version(shader_id, version)
    return {"id": shader_id, "version": document["version"], "tuned_values": _tuned(document)}


def reset_values(conn: sqlite3.Connection, base: Path, shader_id: str, version: int | None = None) -> dict:
    """Back to the authored defaults on one version, default the latest: the tuned values are
    dropped, exports use the defaults."""
    bad = _bad_id(shader_id)
    if bad:
        return bad
    document = record.reset_tuned_values(conn, base, shader_id, version)
    if document is None:
        return _no_version(shader_id, version)
    return {"id": shader_id, "version": document["version"], "tuned_values": _tuned(document)}


def _no_version(shader_id: str, version: int | None) -> dict:
    return {"error": f"no shader {shader_id}" + (f" version {version}" if version is not None else ""),
            "status": 404}


def refresh_thumb(conn: sqlite3.Connection, base: Path, conn_factory, shader_id: str,
                  version: int | None = None) -> dict:
    """After a slider change on the latest version (or no version named): its thumb is re-rendered
    with the new values as a background job. The slider request returns at once. The library
    shows the latest version's thumb, so tuning an older version refreshes nothing."""
    newest = None if _bad_id(shader_id) else record.latest(conn, shader_id)
    if newest is None or (version is not None and int(version) != int(newest["version"])):
        return {}
    return thumbs.start_refresh(conn, base, conn_factory, shader_id)


# ---------------------------------------------------------------- library routes
#
# Answers use camelCase keys, as Desktop reads them. A refused action is a dict with `error` (a
# code), `message` and `status`; plugin_api turns it into `{"detail": {...}}` with that status.

def _refuse(status: int, error: str, message: str, **extra) -> dict:
    return {"error": error, "message": message, "status": status, **extra}


def _library():
    return importlib.import_module("..library", __package__)


def _check_id(shader_id: str) -> dict | None:
    if not record.valid_id(shader_id):
        return _refuse(400, "bad_id", f"invalid shader id {shader_id!r}")
    return None


def _snapshot(conn: sqlite3.Connection, ids=None) -> dict:
    """The latest record, meta, version count and first creation time of every shader, or only of
    `ids` (an answer about one shader reads that one, its parent and its forks)."""
    if ids is None:
        latest = _latest_rows(conn)
        where, params = "", ()
    else:
        wanted = sorted({shader_id for shader_id in ids if shader_id})
        latest = {doc["id"]: doc for doc in (record.latest(conn, shader_id) for shader_id in wanted) if doc}
        where, params = f" WHERE id IN ({','.join('?' * len(wanted))})", tuple(wanted)
        if not wanted:
            return {"latest": {}, "counts": {}, "metas": {}}
    counts = {row[0]: (int(row[1]), row[2] or "") for row in conn.execute(
        "SELECT id, COUNT(*), MIN(CASE WHEN json_valid(record) THEN json_extract(record, '$.created_at') END) "
        f"FROM shaders{where} GROUP BY id", params)}
    metas = {}
    for shader_id, raw in conn.execute(f"SELECT id, meta FROM shader_meta{where}", params):
        try:
            metas[shader_id] = json.loads(raw)
        except ValueError:
            continue
    return {"latest": latest, "counts": counts, "metas": metas}


def _latest_rows(conn: sqlite3.Connection) -> dict:
    """Every shader's latest record without its source and sliders: a row needs neither, and the
    source is most of what the page would otherwise decode."""
    record.ensure_schema(conn)
    out = {}
    for (raw,) in conn.execute(
            "SELECT json_remove(s.record, '$.source', '$.sliders') FROM shaders AS s "
            "JOIN (SELECT id, MAX(version) AS version FROM shaders GROUP BY id) AS newest "
            "ON s.id = newest.id AND s.version = newest.version WHERE json_valid(s.record)"):
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            continue
        out[doc["id"]] = doc
    return out


def _meta(snapshot: dict, conn: sqlite3.Connection, shader_id: str) -> dict:
    if shader_id in snapshot["metas"]:
        return {**record.new_meta(shader_id, shader_id), **snapshot["metas"][shader_id]}
    return record.get_meta(conn, shader_id)


def _row(conn, snapshot: dict, shader_id: str) -> dict:
    return _with_thumb(snapshot, _row_lite(conn, snapshot, shader_id))


def _parent_of(doc: dict | None) -> str | None:
    return ((doc or {}).get("provenance") or {}).get("parent")


def _one(conn, shader_id: str) -> dict:
    """The snapshot an answer about one shader needs: itself and its parent."""
    return _snapshot(conn, [shader_id, _parent_of(record.latest(conn, shader_id))])


def _fork_ids(conn, shader_id: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT id FROM shaders WHERE json_valid(record) "
        "AND json_extract(record, '$.provenance.parent') = ?", (shader_id,))]


def library_page(conn: sqlite3.Connection, base: Path, params: dict) -> dict:
    """`GET /library`: the contract's `LibraryPage`."""
    library = _library()
    query = library.parse_query(params)
    snapshot = _snapshot(conn)
    rows_all = [_row_lite(conn, snapshot, shader_id) for shader_id in snapshot["latest"]]
    window, total, cursor = library.page(rows_all, query)
    pick = library.pick_up(rows_all)
    from .. import jobs
    # Rows carry no image: inline thumbs made a page of 28 rows take 644 ms (median) through the
    # plugin host, so Desktop fetches them in batched GET /thumbs calls instead.
    return {"rows": window, "total": total, "cursor": cursor,
            "adoptable": len(thumbs.adoptable(conn, base)),
            "pickUp": pick,
            "facets": library.facets(rows_all),
            "jobs": [jobs.public(job) for job in jobs.active(conn)]}


def _row_lite(conn, snapshot: dict, shader_id: str) -> dict:
    """A row whose thumb is only checked for existence: the page reads the bytes of the rows it
    returns, not of all of them."""
    latest = snapshot["latest"][shader_id]
    meta = _meta(snapshot, conn, shader_id)
    versions, created = snapshot["counts"].get(shader_id, (1, ""))
    provenance = latest.get("provenance") or {}
    parent = None
    if provenance.get("parent"):
        parent_id = provenance["parent"]
        parent_meta = _meta(snapshot, conn, parent_id) if parent_id in snapshot["latest"] else None
        parent = {"id": parent_id,
                  "name": record.display_name(parent_meta["name"]) if parent_meta else parent_id,
                  "version": provenance.get("parent_version")}
    found = record.thumb_stat(latest)
    return _library().row(latest, meta, versions=versions, created=created, parent=parent,
                          thumb=None, has_thumb=found is not None, thumb_stamp=found[1] if found else None)


def _with_thumb(snapshot: dict, row: dict) -> dict:
    if row["thumbVersion"] is None:
        return row
    return {**row, "thumb": thumbs.data_url(record.thumb_path(snapshot["latest"][row["id"]]))}


MAX_THUMB_IDS = 60


def thumbs_batch(conn: sqlite3.Connection, ids: str) -> dict:
    """`GET /thumbs?ids=<id>@v<n>,...`: the thumbs of up to 60 versions in one answer (a missing
    one is null). Desktop caches them by key, so a thumb is fetched once per version."""
    keys = [key for key in str(ids or "").split(",") if key][:MAX_THUMB_IDS]
    out: dict = {}
    for key in keys:
        shader_id, _, version = key.partition("@v")
        if not record.valid_id(shader_id) or not version.isdigit():
            out[key] = None
            continue
        doc = record.get_version(conn, shader_id, int(version))
        out[key] = thumbs.data_url(record.thumb_path(doc)) if doc else None
    return {"thumbs": out}


def _forks(snapshot: dict, conn, shader_id: str) -> list[dict]:
    out = []
    for other_id, doc in snapshot["latest"].items():
        provenance = doc.get("provenance") or {}
        if provenance.get("parent") == shader_id:
            meta = _meta(snapshot, conn, other_id)
            out.append({"id": other_id, "name": record.display_name(meta["name"]),
                        "version": provenance.get("parent_version")})
    return sorted(out, key=lambda item: item["id"])


def shader_detail(conn: sqlite3.Connection, base: Path, shader_id: str) -> dict:
    """`GET /shaders/{id}`: the contract's `ShaderDetail`."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    newest = record.latest(conn, shader_id)
    if newest is None:
        return _refuse(404, "not_found", f"no shader {shader_id}")
    snapshot = _snapshot(conn, [shader_id, _parent_of(newest), *_fork_ids(conn, shader_id)])
    library = _library()
    meta = _meta(snapshot, conn, shader_id)
    latest = snapshot["latest"][shader_id]
    versions = [library.version_summary(doc, thumb_stamp=(record.thumb_stat(doc) or (None, None))[1])
                for doc in record.history(conn, shader_id)]
    return {"row": _row(conn, snapshot, shader_id),
            "latest": library.version_doc(latest, meta, thumb=thumbs.data_url(record.thumb_path(latest))),
            "versions": versions, "forks": _forks(snapshot, conn, shader_id),
            "licenceOptions": library.licence_options(meta), "description": meta.get("description") or ""}


def version_detail(conn: sqlite3.Connection, shader_id: str, version: int) -> dict:
    """`GET /shaders/{id}/versions/{n}`: the contract's `VersionDoc`."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    doc = record.get_version(conn, shader_id, int(version))
    if doc is None:
        return _refuse(404, "not_found", f"no shader {shader_id} version {version}")
    return _library().version_doc(doc, record.get_meta(conn, shader_id),
                                  thumb=thumbs.data_url(record.thumb_path(doc)))


def compare_versions(conn: sqlite3.Connection, shader_id: str, a: int, b: int) -> dict:
    """`GET /shaders/{id}/compare?a=&b=`: the contract's `Compare`."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    left, right = record.get_version(conn, shader_id, int(a)), record.get_version(conn, shader_id, int(b))
    if left is None or right is None:
        return _refuse(404, "not_found", f"{shader_id} has no version {a if left is None else b}")
    library = _library()
    diff = library.compare(left, right)
    return {"id": shader_id,
            "a": library.version_summary(left, thumb=thumbs.data_url(record.thumb_path(left))),
            "b": library.version_summary(right, thumb=thumbs.data_url(record.thumb_path(right))),
            "lines": diff["lines"], "sliders": diff["sliders"],
            "aSource": left.get("source") or "", "bSource": right.get("source") or ""}


def patch_meta(conn: sqlite3.Connection, base: Path, shader_id: str, body: dict) -> dict:
    """`PATCH /shaders/{id}`: name, tags, starred, description and licence (the
    licence rules in `library.licence_change_refusal`)."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    if record.latest(conn, shader_id) is None:
        return _refuse(404, "not_found", f"no shader {shader_id}")
    if not isinstance(body, dict):
        return _refuse(400, "bad_request", "the body must be a JSON object")
    library = _library()
    changes: dict = {}
    if "name" in body:
        name = library.clean_name(body["name"])
        if name is None:
            return _refuse(400, "bad_request", "the name must be a non-empty string")
        changes["name"] = name
    if "tags" in body:
        tags = library.clean_tags(body["tags"])
        if isinstance(tags, str):
            return _refuse(400, "bad_request", tags)
        changes["tags"] = tags
    if "starred" in body:
        if not isinstance(body["starred"], bool):
            return _refuse(400, "bad_request", "starred must be true or false")
        changes["starred"] = body["starred"]
    if "description" in body:
        if not isinstance(body["description"], str):
            return _refuse(400, "bad_request", "the description must be text")
        changes["description"] = body["description"][:library.DESCRIPTION_LIMIT]
    if "licence" in body:
        licence = body["licence"]
        if licence is not None and not isinstance(licence, str):
            return _refuse(400, "bad_request", "the licence must be an SPDX id or null")
        refusal = library.licence_change_refusal(record.get_meta(conn, shader_id), licence)
        if refusal:
            allowed = [o["id"] for o in library.licence_options(record.get_meta(conn, shader_id)) if o["allowed"]]
            return _refuse(409, "licence_refused", refusal, allowed=allowed)
        if record.set_licence(conn, base, shader_id, licence) is None:
            return _refuse(404, "not_found", f"no shader {shader_id}")
    if changes:
        record.update_meta(conn, shader_id, **changes)
    detail = shader_detail(conn, base, shader_id)
    return {"row": detail["row"], "detail": detail}


def set_archived(conn: sqlite3.Connection, base: Path, shader_id: str, archived: bool) -> dict:
    bad = _check_id(shader_id)
    if bad:
        return bad
    if record.latest(conn, shader_id) is None:
        return _refuse(404, "not_found", f"no shader {shader_id}")
    record.update_meta(conn, shader_id, archived_at=record.now_iso() if archived else None)
    return {"row": _row(conn, _one(conn, shader_id), shader_id)}


def delete_shader(conn: sqlite3.Connection, base: Path, shader_id: str, confirm) -> dict:
    """Delete forever, archived or not, with the name typed. The agent has no delete: only a person deletes."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    if record.latest(conn, shader_id) is None:
        return _refuse(404, "not_found", f"no shader {shader_id}")
    meta = record.get_meta(conn, shader_id)
    names = {meta["name"], record.display_name(meta["name"])}
    if not isinstance(confirm, str) or confirm.strip() not in names:
        return _refuse(400, "confirm_mismatch", "type the shader's name exactly to delete it")
    record.delete(conn, base, shader_id)
    return {"deleted": shader_id}


MAX_BULK_IDS = 5000
BULK_CONFIRM = "delete"


def delete_many(conn: sqlite3.Connection, base: Path, body) -> dict:
    """`POST /library/delete`: the selection bar's delete. Checks everything before it deletes
    anything; an id that is already gone is `missing`, so a repeat ends in the same state."""
    body = body if isinstance(body, dict) else {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > MAX_BULK_IDS \
            or not all(isinstance(shader_id, str) for shader_id in ids):
        return _refuse(400, "bad_request", f"ids must be a list of 1 to {MAX_BULK_IDS} shader ids")
    for shader_id in ids:
        bad = _check_id(shader_id)
        if bad:
            return bad
    if body.get("confirm") != BULK_CONFIRM:
        return _refuse(400, "confirm_mismatch", f"type {BULK_CONFIRM} to delete the selected shaders")
    deleted, missing = [], []
    for shader_id in dict.fromkeys(ids):
        if record.latest(conn, shader_id) is None:
            missing.append(shader_id)
            continue
        record.delete(conn, base, shader_id)
        deleted.append(shader_id)
    return {"deleted": deleted, "missing": missing}


def library_ids(conn: sqlite3.Connection, params: dict) -> dict:
    """`GET /library/ids`: every id the same `/library` query pages through, in its order, for
    Select all across pages that were never loaded."""
    library = _library()
    query = {**library.parse_query(params), "offset": 0}
    snapshot = _snapshot(conn)
    rows_all = [_row_lite(conn, snapshot, shader_id) for shader_id in snapshot["latest"]]
    selected = library.sort_rows([r for r in rows_all if library.matches(r, query)], query["sort"])
    return {"ids": [r["id"] for r in selected], "total": len(selected)}


def desktop_settings() -> dict:
    """`GET /settings`: the one setting the Desktop half reads, fresh on every call."""
    return {"starter_prompt": bool(_settings().get("starter_prompt", True))}


def fork_shader(conn: sqlite3.Connection, base: Path, shader_id: str, body: dict) -> dict:
    bad = _check_id(shader_id)
    if bad:
        return bad
    body = body if isinstance(body, dict) else {}
    version = body.get("version")
    name = _library().clean_name(body.get("name")) if body.get("name") is not None else None
    forked = record.fork(conn, base, shader_id, name=name,
                         version=int(version) if isinstance(version, int) and not isinstance(version, bool) else None)
    if forked is None:
        return _refuse(404, "not_found", f"no shader {shader_id}" + (f" version {version}" if version else ""))
    return {"row": _row(conn, _one(conn, forked["id"]), forked["id"])}


def start_job(conn: sqlite3.Connection, base: Path, conn_factory, kind: str) -> dict:
    """`POST /jobs/thumbs|adopt`: start, resume or return the running job of that kind."""
    if kind == "thumbs":
        job = thumbs.start_backfill(conn, base, conn_factory)
    elif kind == "adopt":
        from .. import render_version
        job = thumbs.start_adopt(conn, base, conn_factory, render_version)
    else:
        return _refuse(404, "not_found", f"no job kind {kind}")
    return {"job": _job_view(conn, base, job)}


def job_status(conn: sqlite3.Connection, base: Path, job_id: str) -> dict:
    from .. import jobs
    job = jobs.public(jobs.get(conn, job_id)) if isinstance(job_id, str) else None
    if job is None:
        return _refuse(404, "not_found", f"no job {job_id}")
    return {"job": _job_view(conn, base, job)}


def _job_view(conn, base: Path, job: dict) -> dict:
    if job.get("kind") == "adopt":
        result = dict(job.get("result") or {})
        result.setdefault("adopted", [])
        result["skipped"] = thumbs.skipped(conn, base) + [
            {"folder": e["item"], "reason": e["reason"]} for e in job.get("errors") or []]
        job = {**job, "result": result}
    return job


# ---------------------------------------------------------------- share files and import

def share(conn: sqlite3.Connection, base: Path, shader_id: str, version: int | None = None) -> dict:
    """`GET /shaders/{id}/share`: `{filename, share}`; Desktop writes it with `ctx.files`."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    from .. import share_file
    answer = share_file(conn, base, shader_id, version, _settings())
    if not answer:
        return _refuse(404, "not_found", f"no shader {shader_id}" + (f" version {version}" if version else ""))
    return answer


def import_from(conn: sqlite3.Connection, base: Path, body: dict) -> dict:
    """`POST /import`: a share file (`{share}`) or a pasted source (`{source, name, url, author,
    licence}`). An inline `{ok: false, ...}` when it does not land; otherwise the new row."""
    if not isinstance(body, dict):
        return _refuse(400, "bad_request", "the body must be a JSON object")
    from .. import import_shader
    answer = import_shader(conn, base, body)
    if not answer.get("ok"):
        return answer
    return {"ok": True, "row": _row(conn, _one(conn, answer["id"]), answer["id"]), "warnings": answer["warnings"],
            "dialect": answer["dialect"]}


# ---------------------------------------------------------------- Lab save

def save_version(conn: sqlite3.Connection, base: Path, shader_id: str, body: dict) -> dict:
    """`POST /shaders/{id}/versions`: `{ok: true, version, row}`, the inline failure, or
    `stale_base` / `busy` 409."""
    bad = _check_id(shader_id)
    if bad:
        return bad
    if not isinstance(body, dict):
        return _refuse(400, "bad_request", "the body must be a JSON object")
    from .. import save_human_version
    answer = save_human_version(conn, base, shader_id, body.get("source"), body.get("base_version"))
    if "refused" in answer:
        status = answer.pop("refused")
        return _refuse(status, answer.pop("error"), answer.pop("message"), **answer)
    if not answer.get("ok"):
        return answer
    return {"ok": True, "version": version_detail(conn, shader_id, answer["version"]),
            "row": _row(conn, _one(conn, shader_id), shader_id), "warning": answer.get("warning")}
