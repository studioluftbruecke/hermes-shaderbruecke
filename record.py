"""Shader records: `@slider` parsing, versions, provenance, storage.

Storage takes a `sqlite3.Connection` and a base `Path` as arguments, never Hermes globals: the
test venv cannot import Hermes, and the same functions run against `plugin_db("shaderbruecke")`
and `plugin_data_dir("shaderbruecke")` in the tool.

Layout: one row per version in table ``shaders(id, version, record)``; files under
``<base>/shaders/<id>/v<version>/``.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

MAX_SLIDERS = 16

# Names the renderer injects, GLSL keywords, and the builtins a slider must not shadow.
RESERVED = frozenset("""
iTime iResolution iMouse iFrame iDate iTimeDelta fragColor fragCoord main mainImage sb_FragColor
void bool int uint float double vec2 vec3 vec4 ivec2 ivec3 ivec4 bvec2 bvec3 bvec4 mat2 mat3 mat4
sampler2D samplerCube const uniform in out inout attribute varying precision highp mediump lowp
return if else for while do break continue discard struct switch case default true false
texture textureLod texture2D texelFetch smoothstep mix clamp step length distance normalize dot
cross sin cos tan asin acos atan pow exp log exp2 log2 sqrt inversesqrt abs sign floor ceil fract
mod min max round trunc fwidth dFdx dFdy
""".split())

_SLIDER_RE = re.compile(
    r"^\s*//\s*@slider\s+(?P<name>[A-Za-z_]\w*)\s+(?P<min>-?\d+(?:\.\d+)?)\s*\.\.\s*"
    r"(?P<max>-?\d+(?:\.\d+)?)\s*=\s*(?P<default>-?\d+(?:\.\d+)?)"
    r"(?:\s+step\s+(?P<step>-?\d+(?:\.\d+)?))?\s*$")


def parse_sliders(source: str) -> list[dict]:
    """Every valid ``// @slider <name> <min>..<max> = <default> [step <s>]`` line, in order.

    Invalid lines are skipped (a slider whose name is a GLSL keyword or an injected uniform would
    shadow it), and at most ``MAX_SLIDERS`` are kept.
    """
    sliders: list[dict] = []
    seen: set[str] = set()
    for line in source.splitlines():
        match = _SLIDER_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name in RESERVED or name in seen or len(sliders) >= MAX_SLIDERS:
            continue
        low, high = float(match.group("min")), float(match.group("max"))
        default = float(match.group("default"))
        if high <= low or not (low <= default <= high):
            continue
        step = match.group("step")
        sliders.append({"name": name, "min": low, "max": high, "default": default,
                        "step": float(step) if step else None})
        seen.add(name)
    return sliders


def slider_header(sliders: list[dict]) -> str:
    """The `uniform float` lines a render or export needs for these sliders."""
    return "".join(f"uniform float {slider['name']};\n" for slider in sliders)


def slider_values(sliders: list[dict]) -> dict[str, float]:
    return {slider["name"]: slider["default"] for slider in sliders}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "shader"


def new_id(name: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}-{_slug(name)}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS shaders (id TEXT, version INTEGER, record TEXT, "
                 "PRIMARY KEY (id, version))")
    conn.commit()


def next_version(conn: sqlite3.Connection, shader_id: str) -> int:
    row = conn.execute("SELECT MAX(version) FROM shaders WHERE id = ?", (shader_id,)).fetchone()
    return int(row[0] or 0) + 1


def save(conn: sqlite3.Connection, record: dict) -> dict:
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO shaders (id, version, record) VALUES (?, ?, ?)",
                 (record["id"], int(record["version"]), json.dumps(record)))
    conn.commit()
    return record


def get_version(conn: sqlite3.Connection, shader_id: str, version: int) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT record FROM shaders WHERE id = ? AND version = ?",
                       (shader_id, int(version))).fetchone()
    return json.loads(row[0]) if row else None


def latest(conn: sqlite3.Connection, shader_id: str) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT record FROM shaders WHERE id = ? ORDER BY version DESC LIMIT 1",
                       (shader_id,)).fetchone()
    return json.loads(row[0]) if row else None


def history(conn: sqlite3.Connection, shader_id: str) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute("SELECT record FROM shaders WHERE id = ? ORDER BY version", (shader_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def list_recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Latest version of every shader, newest first."""
    ensure_schema(conn)
    rows = conn.execute("SELECT record FROM shaders ORDER BY id, version").fetchall()
    newest: dict[str, dict] = {}
    for row in rows:
        record = json.loads(row[0])
        current = newest.get(record["id"])
        if current is None or record["version"] >= current["version"]:
            newest[record["id"]] = record
    ordered = sorted(newest.values(), key=lambda r: r.get("created_at", ""), reverse=True)
    return ordered[:limit]


def version_dir(base: Path, shader_id: str, version: int) -> Path:
    return Path(base) / "shaders" / shader_id / f"v{version}"


def fork(conn: sqlite3.Connection, base: Path, shader_id: str, name: str | None = None) -> dict | None:
    """A new id whose version 1 is the latest version of `shader_id`, with lineage recorded.

    The latest version's files are copied too, so the fork has its own `shader.frag` and frames.
    """
    source = latest(conn, shader_id)
    if source is None:
        return None
    record = json.loads(json.dumps(source))
    record["id"] = new_id(name or source.get("name") or "fork")
    record["version"] = 1
    record["name"] = name or source.get("name") or "fork"
    record["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    provenance = dict(record.get("provenance") or {})
    provenance["parent"] = shader_id
    provenance["origin"] = provenance.get("origin") or "agent"
    record["provenance"] = provenance
    origin_dir = version_dir(base, shader_id, source["version"])
    target_dir = version_dir(base, record["id"], 1)
    if origin_dir.is_dir():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(origin_dir, target_dir, dirs_exist_ok=True)
        record["files"] = {key: str(Path(value).relative_to(origin_dir)) if str(value).startswith(str(origin_dir)) else value
                           for key, value in (record.get("files") or {}).items()}
    return save(conn, record)


def set_slider_values(conn: sqlite3.Connection, shader_id: str, values: dict[str, float]) -> dict | None:
    """Store the human's slider values on the latest version (Desktop posts them)."""
    record = latest(conn, shader_id)
    if record is None:
        return None
    allowed = {slider["name"] for slider in record.get("sliders") or []}
    merged = dict(record.get("slider_values") or {})
    for name, value in values.items():
        if name in allowed and isinstance(value, (int, float)):
            merged[name] = float(value)
    record["slider_values"] = merged
    record["edited_by"] = "human"
    return save(conn, record)
