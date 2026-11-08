"""FastAPI router for the shaderbruecke pane (`/api/plugins/shaderbruecke/...`).

Mounted only because `dashboard/manifest.json` declares `api`. The logic lives in `library_api.py`
so it is testable without FastAPI or Hermes; this module only binds it to the profile's storage.

Hermes imports this file by path, not as part of a package, so the package import below puts the
plugin's parent folder on `sys.path` first; relative imports would fail with "no known parent
package" (measured: the same failure the plugin tests hit when they load a module by path).
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

from shaderbruecke.dashboard import library_api  # noqa: E402

router = APIRouter()


def _storage():
    from plugins.plugin_storage import plugin_data_dir, plugin_db
    return plugin_db("shaderbruecke"), plugin_data_dir("shaderbruecke")


@router.get("/shaders/{shader_id}/targets")
def get_targets(shader_id: str, version: int | None = None) -> dict:
    conn, base = _storage()
    payload = library_api.targets_for(conn, base, shader_id, version)
    if "error" in payload:
        raise HTTPException(status_code=404, detail=payload["error"])
    return payload


@router.get("/shaders/{shader_id}/export/{target}")
def get_export(shader_id: str, target: str, version: int | None = None) -> dict:
    conn, base = _storage()
    payload = library_api.export_text(conn, base, shader_id, target, version)
    if "error" in payload:
        raise HTTPException(status_code=409 if payload.get("needs_export") else 404,
                            detail=payload["error"])
    return payload


class SliderValues(BaseModel):
    values: dict[str, float]


@router.post("/shaders/{shader_id}/slider_values")
def post_slider_values(shader_id: str, payload: SliderValues) -> dict:
    conn, _base = _storage()
    result = library_api.set_values(conn, shader_id, payload.values)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
