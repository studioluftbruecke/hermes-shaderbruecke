"""Shader records: `@slider` parsing, versions, provenance, storage.

Storage takes a `sqlite3.Connection` and a base `Path` as arguments, never Hermes globals: the
test venv cannot import Hermes, and the same functions run against `plugin_db("shaderbruecke")`
and `plugin_data_dir("shaderbruecke")` in the tool.

Layout: one row per version in table ``shaders(id, version, record)``; files under
``<base>/shaders/<id>/v<version>/``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import sqlite3
import stat
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
    r"(?:\s+step\s+(?P<step>-?\d+(?:\.\d+)?))?"
    r'(?:\s+"(?P<label>[^"]*)")?\s*$')
LABEL_LIMIT = 60


def parse_sliders(source: str) -> list[dict]:
    """Every valid ``// @slider <name> <min>..<max> = <default> [step <s>] ["Label"]`` line.

    Invalid lines are skipped (a slider whose name is a GLSL keyword or an injected uniform would
    shadow it), and at most ``MAX_SLIDERS`` are kept. A slider carries ``label`` only when its
    line names one; readers treat a missing key as no label.
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
        slider = {"name": name, "min": low, "max": high, "default": default,
                  "step": float(step) if step else None}
        label = " ".join((match.group("label") or "").split())[:LABEL_LIMIT]
        if label:
            slider["label"] = label
        sliders.append(slider)
        seen.add(name)
    return sliders


def slider_header(sliders: list[dict]) -> str:
    """The `uniform float` lines a render or export needs for these sliders."""
    return "".join(f"uniform float {slider['name']};\n" for slider in sliders)


def slider_values(sliders: list[dict]) -> dict[str, float]:
    """The authored defaults of these sliders."""
    return {slider["name"]: slider["default"] for slider in sliders}


def _clamped(slider: dict, value) -> float | None:
    """`value` as a float inside the slider's range, or None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return min(float(slider["max"]), max(float(slider["min"]), number))


def tuned_values(doc: dict) -> dict[str, float]:
    """The human's values for the record's sliders, clamped; names without one are absent.

    Rows written before `tuned_values` existed carry `slider_values`, read as the fallback.
    """
    raw = doc.get("tuned_values") if "tuned_values" in doc else doc.get("slider_values")
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for slider in doc.get("sliders") or []:
        if slider["name"] in raw:
            value = _clamped(slider, raw[slider["name"]])
            if value is not None:
                out[slider["name"]] = value
    return out


def effective_values(doc: dict) -> dict[str, float]:
    """Per slider: the tuned value when the human set one, else the authored default."""
    tuned = tuned_values(doc)
    return {slider["name"]: tuned.get(slider["name"], float(slider["default"]))
            for slider in doc.get("sliders") or []}


def with_effective_defaults(doc: dict) -> list[dict]:
    """The record's sliders with `default` replaced by the effective value and the authored one
    kept as `authored`. Exporters write these, so a tuned look is the export's default."""
    values = effective_values(doc)
    return [{**slider, "default": values[slider["name"]], "authored": slider["default"]}
            for slider in doc.get("sliders") or []]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "shader"


_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


def valid_id(shader_id) -> bool:
    """True for a shader id that is safe as one path segment: ``^[a-z0-9][a-z0-9-]{0,79}$``."""
    return isinstance(shader_id, str) and _ID_RE.fullmatch(shader_id) is not None


def new_id(name: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}-{_slug(name)}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS shaders (id TEXT, version INTEGER, record TEXT, "
                 "PRIMARY KEY (id, version))")
    conn.execute("CREATE TABLE IF NOT EXISTS shader_meta (id TEXT PRIMARY KEY, meta TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS human_notices (id TEXT, version INTEGER, created_at TEXT, "
                 "delivered_at TEXT, PRIMARY KEY (id, version))")
    conn.commit()


def next_version(conn: sqlite3.Connection, shader_id: str) -> int:
    ensure_schema(conn)
    row = conn.execute("SELECT MAX(version) FROM shaders WHERE id = ?", (shader_id,)).fetchone()
    return int(row[0] or 0) + 1


def save(conn: sqlite3.Connection, record: dict) -> dict:
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO shaders (id, version, record) VALUES (?, ?, ?)",
                 (record["id"], int(record["version"]), json.dumps(record)))
    conn.commit()
    return record


def insert(conn: sqlite3.Connection, record: dict) -> dict:
    """A new version. Unlike `save` it never replaces a row: a second writer that got the same
    number raises instead of overwriting (callers number versions inside `shader_lock`)."""
    ensure_schema(conn)
    conn.execute("INSERT INTO shaders (id, version, record) VALUES (?, ?, ?)",
                 (record["id"], int(record["version"]), json.dumps(record)))
    conn.commit()
    return record


@contextlib.contextmanager
def shader_lock(base: Path, shader_id: str):
    """Serialise numbering and storing a new version of one shader across threads and processes
    (an agent turn, a Lab save, a job). The render itself runs outside the lock."""
    shader_dir(base, shader_id)               # validates the id
    folder = Path(base) / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / f"shader-{shader_id}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


def recent_versions(conn: sqlite3.Connection, shader_id: str, *, within_s: float,
                    now: float | None = None) -> int:
    """How many versions of `shader_id` were made in the last `within_s` seconds."""
    import calendar
    cutoff = (time.time() if now is None else now) - within_s
    count = 0
    for doc in history(conn, shader_id):
        try:
            made = calendar.timegm(time.strptime(str(doc.get("created_at")), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        count += made >= cutoff
    return count


def list_recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Latest version of every shader, newest first. Only those rows are read and decoded."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT s.record FROM shaders AS s "
        "JOIN (SELECT id, MAX(version) AS version FROM shaders GROUP BY id) AS newest "
        "ON s.id = newest.id AND s.version = newest.version "
        "ORDER BY CASE WHEN json_valid(s.record) THEN json_extract(s.record, '$.created_at') END DESC, "
        "s.id LIMIT ?", (int(limit),)).fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row[0]))
        except ValueError:
            continue
    return out


# Files a version keeps for good: its source, its result, and every export's deliverables and
# proof.json. Frames, contact sheets, proof PNGs and browser logs are regenerated by a render.
_PRUNABLE_SUFFIXES = (".png", ".log")
_KEPT_NAMES = frozenset({"reference-t1.png"})


def prune_versions(base: Path, shader_id: str, keep: int) -> int:
    """Delete the frames and proof images of every version but the newest `keep`.

    Records and sources stay; a pruned version can be rendered again from its `shader.frag`.
    Returns the number of files removed.
    """
    folder = shader_dir(base, shader_id)
    if not folder.is_dir():
        return 0
    versions = sorted((int(child.name[1:]), child) for child in folder.iterdir()
                      if child.is_dir() and child.name[:1] == "v" and child.name[1:].isdigit())
    removed = 0
    for _number, version_folder in versions[:max(0, len(versions) - max(1, int(keep)))]:
        for path in sorted(version_folder.rglob("*"), reverse=True):
            if path.is_file() and path.suffix in _PRUNABLE_SUFFIXES and path.name not in _KEPT_NAMES:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return removed


def shader_dir(base: Path, shader_id: str) -> Path:
    """``<base>/shaders/<id>``, refused unless the id is a slug and the path stays under base."""
    if not valid_id(shader_id):
        raise ValueError(f"invalid shader id {shader_id!r}: use lowercase letters, digits and '-'")
    root = (Path(base) / "shaders").resolve()
    path = (root / shader_id).resolve()
    if path.parent != root:
        raise ValueError(f"shader id {shader_id!r} leaves the shader folder")
    return path


def version_dir(base: Path, shader_id: str, version: int) -> Path:
    return shader_dir(base, shader_id) / f"v{int(version)}"


def _scratch_root(base: Path, shader_id: str) -> Path:
    shader_dir(base, shader_id)               # validates the id
    return Path(base).resolve() / "scratch" / shader_id


def attempt_dir(base: Path, shader_id: str) -> Path:
    """A fresh folder for one render attempt, outside the version folders.

    A failed render (lint, compile, timeout) stays here for inspection; a successful one is moved
    to its version folder.
    """
    return _scratch_root(base, shader_id) / f"{time.time_ns()}"


ATTEMPT_META = "attempt.json"


def write_attempt_meta(out_dir: Path, *, parent_id: str | None) -> None:
    """What a later call needs to continue this attempt's id: its lineage."""
    (Path(out_dir) / ATTEMPT_META).write_text(json.dumps({"parent_id": parent_id}), encoding="utf-8")


def attempt_meta(base: Path, shader_id: str) -> dict | None:
    """The newest failed attempt's metadata for an id with no version yet; None when the plugin
    never minted that id (so `shader_render(id=...)` still refuses ids nobody rendered)."""
    root = _scratch_root(base, shader_id)
    if not root.is_dir():
        return None
    for attempt in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        meta = attempt / ATTEMPT_META
        if meta.is_file():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            parent = data.get("parent_id")
            return {"parent_id": parent if isinstance(parent, str) and valid_id(parent) else None}
    return None


ATTEMPT_RUNNING_S = 900.0


def prune_attempts(base: Path, shader_id: str, keep: Path | None, now: float | None = None) -> None:
    """Remove the finished attempt folders of `shader_id` except `keep` (None keeps none).

    An attempt without `result.json` is still rendering in another call; it is left alone until it
    is older than `ATTEMPT_RUNNING_S` (a process that died mid-render)."""
    root = _scratch_root(base, shader_id)
    if not root.is_dir():
        return
    stamp = time.time() if now is None else now
    for child in root.iterdir():
        if keep is not None and child.resolve() == Path(keep).resolve():
            continue
        if child.is_dir():
            try:
                running = not (child / "result.json").exists() and stamp - child.stat().st_mtime < ATTEMPT_RUNNING_S
            except OSError:
                continue
            if not running:
                shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    with contextlib.suppress(OSError):        # another attempt may have just started here
        root.rmdir()


def rebase(value, origin: str, target: str):
    """Every string path under `origin` moved to the same place under `target`, recursively."""
    if isinstance(value, str):
        if value == origin or value.startswith(origin + "/"):
            return target + value[len(origin):]
        return value
    if isinstance(value, list):
        return [rebase(item, origin, target) for item in value]
    if isinstance(value, dict):
        return {key: rebase(item, origin, target) for key, item in value.items()}
    return value


def fork(conn: sqlite3.Connection, base: Path, shader_id: str, name: str | None = None,
         version: int | None = None) -> dict | None:
    """A new id whose version 1 is `version` of `shader_id` (default: the latest), with lineage
    recorded (`provenance.parent`, `provenance.parent_version`).

    The version's files are copied too, so the fork has its own `shader.frag`, frames and thumb.
    Every absolute path in `files` and `checks` is moved to the fork's own folder. The fork's meta
    starts with the parent's licence and inherited licence; its version keeps the stamped licence
    of the version it was made from.
    """
    source = latest(conn, shader_id) if version is None else get_version(conn, shader_id, version)
    if source is None:
        return None
    parent_meta = get_meta(conn, shader_id)
    if name is None:
        name = parent_meta["name"]
    record = json.loads(json.dumps(source))
    record["id"] = new_id(name or source.get("name") or "fork")
    record["version"] = 1
    record["name"] = name or source.get("name") or "fork"
    record["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    provenance = dict(record.get("provenance") or {})
    provenance["parent"] = shader_id
    provenance["parent_version"] = int(source["version"])
    provenance["origin"] = provenance.get("origin") or "agent"
    record["provenance"] = provenance
    record["author_kind"] = author_kind(source)
    origin_dir = version_dir(base, shader_id, source["version"])
    target_dir = version_dir(base, record["id"], 1)
    if origin_dir.is_dir():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(origin_dir, target_dir, dirs_exist_ok=True)
        for key in ("files", "checks"):
            # Paths were stored either resolved or as built from `base`; move both spellings.
            value = record.get(key)
            for spelling in {str(origin_dir), str(Path(base) / "shaders" / shader_id / origin_dir.name)}:
                value = rebase(value, spelling, str(target_dir))
            record[key] = value
    save_meta(conn, {**new_meta(record["id"], record["name"]),
                     "licence": parent_meta["licence"],
                     "licence_inherited": parent_meta["licence_inherited"]})
    return save(conn, record)


def set_slider_values(conn: sqlite3.Connection, shader_id: str, values: dict[str, float],
                      version: int | None = None) -> dict | None:
    """Merge the human's slider values into `tuned_values` of one version (default: the latest).

    Only names the record declares are kept, only finite numbers, clamped to the slider's range.
    `slider_values` mirrors the result for readers written before `tuned_values` existed.
    """
    record = latest(conn, shader_id) if version is None else get_version(conn, shader_id, version)
    if record is None:
        return None
    sliders = {slider["name"]: slider for slider in record.get("sliders") or []}
    merged = tuned_values(record)
    for name, value in (values or {}).items():
        if name in sliders:
            number = _clamped(sliders[name], value)
            if number is not None:
                merged[name] = number
    record["tuned_values"] = merged
    record["slider_values"] = dict(merged)
    record["edited_by"] = "human"
    return save(conn, record)


def reset_tuned_values(conn: sqlite3.Connection, shader_id: str, version: int | None = None) -> dict | None:
    """Forget the human's values on one version (default: the latest): every slider is back at its
    default."""
    record = latest(conn, shader_id) if version is None else get_version(conn, shader_id, version)
    if record is None:
        return None
    record["tuned_values"] = {}
    record["slider_values"] = {}
    record["edited_by"] = "human"
    return save(conn, record)


# ---------------------------------------------------------------- v0.2: authorship, meta, licences

AUTHOR_KINDS = ("agent", "human", "import")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def author_kind(doc: dict) -> str:
    """Who wrote this version's source. Records from before v0.2 carry no `author_kind`; the agent
    wrote those (an `edited_by: human` there means slider tuning, not new source)."""
    kind = doc.get("author_kind")
    if kind in AUTHOR_KINDS:
        return kind
    return "import" if (doc.get("provenance") or {}).get("origin") == "import" else "agent"


def display_name(name) -> str:
    """Stored names can carry an em or en dash (the agent writes them); shown with a hyphen."""
    return str(name or "").replace("\u2014", "-").replace("\u2013", "-")


def new_meta(shader_id: str, name: str) -> dict:
    return {"id": shader_id, "name": str(name or "shader")[:80], "tags": [], "licence": None,
            "licence_inherited": None, "starred": False, "archived_at": None, "description": "",
            "updated_at": now_iso()}


def get_meta(conn: sqlite3.Connection, shader_id: str) -> dict:
    """The shader's meta; a shader without a meta row (made before v0.2) reads as the defaults,
    named after its latest version."""
    ensure_schema(conn)
    row = conn.execute("SELECT meta FROM shader_meta WHERE id = ?", (shader_id,)).fetchone()
    if row:
        try:
            meta = json.loads(row[0])
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            defaults = new_meta(shader_id, meta.get("name") or shader_id)
            return {**defaults, **meta, "id": shader_id}
    newest = latest(conn, shader_id)
    meta = new_meta(shader_id, (newest or {}).get("name") or shader_id)
    meta["updated_at"] = (newest or {}).get("created_at") or meta["updated_at"]
    return meta


def save_meta(conn: sqlite3.Connection, meta: dict) -> dict:
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO shader_meta (id, meta) VALUES (?, ?)",
                 (meta["id"], json.dumps(meta)))
    conn.commit()
    return meta


def update_meta(conn: sqlite3.Connection, shader_id: str, **changes) -> dict:
    meta = get_meta(conn, shader_id)
    meta.update(changes)
    meta["updated_at"] = now_iso()
    return save_meta(conn, meta)


def set_licence(conn: sqlite3.Connection, shader_id: str, licence: str | None) -> dict | None:
    """Constraint 8: the picker's value becomes the default for new versions and is re-stamped on
    the latest version; earlier versions keep the licence they were made with. The caller checks
    the licence rules (`library.licence_change_refusal`) first."""
    newest = latest(conn, shader_id)
    if newest is None:
        return None
    provenance = dict(newest.get("provenance") or {})
    provenance["licence"] = licence
    newest["provenance"] = provenance
    save(conn, newest)
    return update_meta(conn, shader_id, licence=licence)


def thumb_stat(doc: dict) -> tuple[str, int] | None:
    """The version's `thumb.webp` and when it was written (ms), from one `stat` (the library page
    asks this for every shader)."""
    files = doc.get("files") or {}
    frag = files.get("frag")
    for candidate in (files.get("thumb"), os.path.join(os.path.dirname(frag), "thumb.webp") if frag else None):
        if not candidate:
            continue
        try:
            found = os.stat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(found.st_mode):
            return candidate, found.st_mtime_ns // 1_000_000
    return None


def thumb_path(doc: dict) -> Path | None:
    """The version's `thumb.webp` when it exists on disk."""
    found = thumb_stat(doc)
    return Path(found[0]) if found else None


def all_versions(conn: sqlite3.Connection) -> list[dict]:
    ensure_schema(conn)
    out = []
    for (raw,) in conn.execute("SELECT record FROM shaders ORDER BY id, version"):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def shader_ids(conn: sqlite3.Connection) -> list[str]:
    ensure_schema(conn)
    return [row[0] for row in conn.execute("SELECT DISTINCT id FROM shaders")]


# ---------------------------------------------------------------- v0.2: flat folders (D14)

_LEGACY_ID_RE = re.compile(r"^(?P<date>\d{8})-(?P<time>\d{6})-\d{6}-(?P<slug>[a-z0-9-]+)$")


def legacy_folders(conn: sqlite3.Connection, base: Path) -> list[dict]:
    """Shader folders with no record and the flat layout of the first builds (`result.json` and
    `shader.frag` at the folder root, no `v<n>`): `{folder, status, adoptable, reason, name,
    created_at}`. Only an `ok` render is adoptable; nothing here deletes or changes a folder."""
    root = Path(base) / "shaders"
    if not root.is_dir():
        return []
    recorded = set(shader_ids(conn))
    out = []
    for name in sorted(os.listdir(root)):
        if name in recorded or not valid_id(name):
            continue
        folder = root / name
        if not folder.is_dir():
            continue
        result_file, frag = folder / "result.json", folder / "shader.frag"
        if not result_file.is_file() or not frag.is_file():
            continue
        try:
            status = json.loads(result_file.read_text(encoding="utf-8")).get("status") or "unknown"
        except (OSError, ValueError):
            status = "unreadable"
        match = _LEGACY_ID_RE.match(folder.name)
        slug = match.group("slug") if match else folder.name
        created = (f"{match.group('date')[:4]}-{match.group('date')[4:6]}-{match.group('date')[6:]}T"
                   f"{match.group('time')[:2]}:{match.group('time')[2:4]}:{match.group('time')[4:]}Z"
                   if match else now_iso())
        out.append({"folder": folder.name, "status": status, "adoptable": status == "ok",
                    "reason": None if status == "ok" else
                    f"its only render ended in {status.replace('_', ' ')}, so there is nothing to adopt",
                    "name": slug.replace("-", " "), "created_at": created})
    return out


def delete(conn: sqlite3.Connection, base: Path, shader_id: str) -> None:
    """Every version, the meta, the pending Lab notices and the files of one shader. Under the
    shader's lock, so a render that lands meanwhile finds the shader gone instead of storing it
    again as v1. Files an export copied into a project folder are not ours and stay."""
    folder = shader_dir(base, shader_id)
    ensure_schema(conn)
    with shader_lock(base, shader_id):
        conn.execute("DELETE FROM shaders WHERE id = ?", (shader_id,))
        conn.execute("DELETE FROM shader_meta WHERE id = ?", (shader_id,))
        conn.execute("DELETE FROM human_notices WHERE id = ?", (shader_id,))
        conn.commit()
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(_scratch_root(base, shader_id), ignore_errors=True)


# ---------------------------------------------------------------- v0.2: Lab saves reach the agent (F40)

def add_human_notice(conn: sqlite3.Connection, shader_id: str, version: int) -> None:
    ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO human_notices (id, version, created_at, delivered_at) VALUES (?, ?, ?, NULL)",
                 (shader_id, int(version), now_iso()))
    conn.commit()


def take_human_notices(conn: sqlite3.Connection) -> list[dict]:
    """The Lab saves the agent has not been told about, newest version per shader; marked as told."""
    ensure_schema(conn)
    taken = conn.execute("UPDATE human_notices SET delivered_at = ? WHERE delivered_at IS NULL "
                         "RETURNING id, version, created_at", (now_iso(),)).fetchall()
    conn.commit()
    newest: dict[str, tuple[int, str]] = {}
    for shader_id, version, created in taken:
        if shader_id not in newest or int(version) > newest[shader_id][0]:
            newest[shader_id] = (int(version), created or "")
    ordered = sorted(newest.items(), key=lambda item: item[1][1])
    return [{"id": shader_id, "version": version} for shader_id, (version, _) in ordered]
