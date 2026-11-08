"""The plugin API's logic, free of FastAPI and Hermes so a test can call it directly.

`plugin_api.py` is the thin FastAPI layer; everything here takes a `sqlite3.Connection` and a base
`Path` (the same seam as `record.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import record
from ..exporters import handoff, registry
from ..exporters.header import pascal


def _document(conn: sqlite3.Connection, shader_id: str, version: int | None) -> dict | None:
    if version is not None:
        return record.get_version(conn, shader_id, int(version))
    return record.latest(conn, shader_id)


def targets_for(conn: sqlite3.Connection, base: Path, shader_id: str,
                version: int | None = None) -> dict:
    """The export registry as JSON, plus which targets this shader already has on disk."""
    document = _document(conn, shader_id, version)
    if document is None:
        return {"error": f"no shader {shader_id}"}
    directory = record.version_dir(base, shader_id, document["version"]) / "exports"
    rows = []
    for target_id, row in registry.EXPORT_TARGETS.items():
        main = row.main_file.format(name=pascal(document.get("name")))
        rows.append({"id": target_id, "label": row.label, "main_file": main,
                     "proof": row.proof, "status": row.status,
                     "exported": (directory / target_id / main).is_file()})
    return {"id": shader_id, "version": document["version"], "targets": rows}


def export_text(conn: sqlite3.Connection, base: Path, shader_id: str, target: str,
                version: int | None = None) -> dict:
    """The text of a target's main file, from the export folder (the Desktop copies it)."""
    row = registry.EXPORT_TARGETS.get(target)
    if row is None:
        return {"error": f"unknown target {target}"}
    document = _document(conn, shader_id, version)
    if document is None:
        return {"error": f"no shader {shader_id}"}
    if target == "agent":
        text = handoff.handoff(document)
        return {"id": shader_id, "version": document["version"], "target": target,
                "main_file": row.main_file.format(name=pascal(document.get("name"))), "text": text}
    path = (record.version_dir(base, shader_id, document["version"]) / "exports" / target
            / row.main_file.format(name=pascal(document.get("name"))))
    if not path.is_file():
        return {"error": f"{target} has not been exported yet", "needs_export": True,
                "id": shader_id, "version": document["version"]}
    return {"id": shader_id, "version": document["version"], "target": target,
            "main_file": path.name, "text": path.read_text(encoding="utf-8")}


def set_values(conn: sqlite3.Connection, shader_id: str, values: dict) -> dict:
    document = record.set_slider_values(conn, shader_id, values)
    if document is None:
        return {"error": f"no shader {shader_id}"}
    return {"id": shader_id, "version": document["version"],
            "slider_values": document.get("slider_values") or {}}
