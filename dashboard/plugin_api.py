"""FastAPI router for the shaderbruecke pane (`/api/plugins/shaderbruecke/...`).

Mounted only because `dashboard/manifest.json` declares `api`. The logic lives in `library_api.py`
so it is testable without FastAPI or Hermes; this module only binds it to the profile's storage
and turns an error dict into its HTTP status.

Hermes imports this file by path, not as part of a package, so the package import below puts the
plugin's parent folder on `sys.path` first; relative imports would fail with "no known parent
package" (measured: the same failure the plugin tests hit when they load a module by path).
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

from shaderbruecke import record  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402

router = APIRouter()


def _storage():
    from plugins.plugin_storage import plugin_data_dir, plugin_db
    conn = plugin_db("shaderbruecke")
    record.ensure_schema(conn)
    return conn, plugin_data_dir("shaderbruecke")


def _answer(payload: dict) -> dict:
    if "error" in payload:
        raise HTTPException(status_code=int(payload.get("status") or 404), detail=payload["error"])
    return payload


@router.get("/shaders/{shader_id}/targets")
def get_targets(shader_id: str, version: int | None = None) -> dict:
    conn, base = _storage()
    try:
        return _answer(library_api.targets_for(conn, base, shader_id, version))
    finally:
        conn.close()


@router.get("/shaders/{shader_id}/export/{target}")
def get_export(shader_id: str, target: str, version: int | None = None) -> dict:
    conn, base = _storage()
    try:
        return _answer(library_api.export_text(conn, base, shader_id, target, version))
    finally:
        conn.close()


@router.post("/shaders/{shader_id}/export/{target}")
def post_export(shader_id: str, target: str, version: int | None = None) -> dict:
    """Export and prove from the card. Slow (a proof starts Chromium): the card waits up to 3 min."""
    conn, base = _storage()
    try:
        return _answer(library_api.export_now(conn, base, shader_id, target, version))
    finally:
        conn.close()


class SliderValues(BaseModel):
    values: dict[str, float]


def _connect():
    """A new connection for a job thread (the request's connection stays with the request)."""
    from plugins.plugin_storage import plugin_db
    return plugin_db("shaderbruecke")


@router.post("/shaders/{shader_id}/slider_values")
def post_slider_values(shader_id: str, payload: SliderValues) -> dict:
    conn, base = _storage()
    try:
        answer = _answer(library_api.set_values(conn, shader_id, payload.values))
        library_api.refresh_thumb(conn, base, _connect, shader_id)
        return answer
    finally:
        conn.close()


@router.delete("/shaders/{shader_id}/slider_values")
def delete_slider_values(shader_id: str) -> dict:
    conn, base = _storage()
    try:
        answer = _answer(library_api.reset_values(conn, shader_id))
        library_api.refresh_thumb(conn, base, _connect, shader_id)
        return answer
    finally:
        conn.close()


# ---------------------------------------------------------------- v0.2 library routes
# Shapes: experiments/shaderbruecke/v02-contract.md (not exported). A refusal answers its status with
# `{"detail": {"error": <code>, "message": <sentence>, ...}}`.

def _reply(payload: dict) -> dict:
    if "error" in payload and "status" in payload:
        status = int(payload.pop("status"))
        raise HTTPException(status_code=status, detail=payload)
    return payload


@router.get("/library")
def get_library(request: Request) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.library_page(conn, base, dict(request.query_params)))
    finally:
        conn.close()


@router.get("/thumbs")
def get_thumbs(ids: str = "") -> dict:
    conn, _base = _storage()
    try:
        return _reply(library_api.thumbs_batch(conn, ids))
    finally:
        conn.close()


@router.get("/shaders/{shader_id}")
def get_shader(shader_id: str) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.shader_detail(conn, base, shader_id))
    finally:
        conn.close()


@router.get("/shaders/{shader_id}/versions/{version}")
def get_version(shader_id: str, version: int) -> dict:
    conn, _base = _storage()
    try:
        return _reply(library_api.version_detail(conn, shader_id, version))
    finally:
        conn.close()


@router.get("/shaders/{shader_id}/compare")
def get_compare(shader_id: str, a: int, b: int) -> dict:
    conn, _base = _storage()
    try:
        return _reply(library_api.compare_versions(conn, shader_id, a, b))
    finally:
        conn.close()


@router.patch("/shaders/{shader_id}")
def patch_shader(shader_id: str, payload: dict = Body(default={})) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.patch_meta(conn, base, shader_id, payload))
    finally:
        conn.close()


@router.post("/shaders/{shader_id}/archive")
def post_archive(shader_id: str) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.set_archived(conn, base, shader_id, True))
    finally:
        conn.close()


@router.post("/shaders/{shader_id}/unarchive")
def post_unarchive(shader_id: str) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.set_archived(conn, base, shader_id, False))
    finally:
        conn.close()


@router.delete("/shaders/{shader_id}")
def delete_shader(shader_id: str, payload: dict = Body(default={})) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.delete_shader(conn, base, shader_id, (payload or {}).get("confirm")))
    finally:
        conn.close()


@router.post("/shaders/{shader_id}/fork")
def post_fork(shader_id: str, payload: dict = Body(default={})) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.fork_shader(conn, base, shader_id, payload))
    finally:
        conn.close()


@router.post("/jobs/{kind}")
def post_job(kind: str) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.start_job(conn, base, _connect, kind))
    finally:
        conn.close()


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.job_status(conn, base, job_id))
    finally:
        conn.close()


@router.get("/shaders/{shader_id}/share")
def get_share(shader_id: str, version: int | None = None) -> dict:
    conn, base = _storage()
    try:
        return _reply(library_api.share(conn, base, shader_id, version))
    finally:
        conn.close()


@router.post("/import")
def post_import(payload: dict = Body(default={})) -> dict:
    """Lint and render before anything lands (up to a few seconds per render): Desktop waits 3 min."""
    conn, base = _storage()
    try:
        return _reply(library_api.import_from(conn, base, payload))
    finally:
        conn.close()


@router.post("/shaders/{shader_id}/versions")
def post_version(shader_id: str, payload: dict = Body(default={})) -> dict:
    """The Lab's Save: lint and a server render before the version lands (Desktop waits 3 min)."""
    conn, base = _storage()
    try:
        return _reply(library_api.save_version(conn, base, shader_id, payload))
    finally:
        conn.close()
