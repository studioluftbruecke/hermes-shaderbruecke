"""Export one target of a shader: write the files, prove them, and only then copy them out.

`export_shader` is the single implementation behind the `shader_export` tool and the dashboard
routes. It takes the connection and the data directory as arguments, so it runs without Hermes.

Order matters. The files land in the shader's own `exports/<target>/` folder, the proof runs on
exactly those files, the verdict is written next to them as `proof.json`, and a project folder
(`dest_dir`) is written last: on `match`, or on any verdict when the caller passes
`accept_unproven=True`, which the result then says in words.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from . import record, renderer
from . import settings as settings_mod
from .exporters import registry, tsl
from .exporters.header import licence_text, pascal

PROOF_MEAN_LIMIT = 2.0     # 2/255
PROOF_P99_LIMIT = 8.0      # 8/255
PROOF_FILE = "proof.json"
HARNESS = Path(__file__).with_name("headless") / "harness.html"


# --- dest_dir ------------------------------------------------------------------------------------

def dest_decision(dest, filenames, *, overwrite: bool = False, allow_writes: bool = False,
                  reason: str | None = None) -> dict:
    """Pure rule for `dest_dir`: absolute, existing, no clash unless overwrite, no unattended write.

    Returns ``{"ok": bool, "error": str|None}``. Kept free of filesystem writes and Hermes imports
    so the refusal cases are unit tested.
    """
    if reason and not allow_writes:
        return {"ok": False, "error": (f"dest_dir is refused in an unattended run ({reason}) unless "
                                       "allow_project_writes is on")}
    if not dest:
        return {"ok": True, "error": None}
    path = Path(str(dest))
    if not path.is_absolute():
        return {"ok": False, "error": "dest_dir must be an absolute path"}
    if not path.is_dir():
        return {"ok": False, "error": f"dest_dir {path} does not exist or is not a directory"}
    clashes = [name for name in filenames if (path / name).exists()]
    if clashes and not overwrite:
        return {"ok": False, "error": f"dest_dir already has {', '.join(clashes)}; "
                                      "pass overwrite=true to replace"}
    return {"ok": True, "error": None, "clashes": clashes}


# --- the harness ---------------------------------------------------------------------------------

def harness(expression: str, out_dir: Path, timeout_s: float = 90.0) -> dict:
    """Evaluate `expression` (an async function body) in the offline three.js harness page."""
    return renderer.run_page(HARNESS, "(async () => { const H = window.sbHarness;"
                                      " if (!H || !H.ready) return {status: 'not_ready'};"
                                      f" return await (async () => {{ {expression} }})(); }})()",
                             timeout_s=timeout_s, out_dir=out_dir, phase="proof")


def raw_transpile(document: dict, out_dir: Path) -> dict:
    """three r187's raw transpiler output for the record's source, or ``{"error": ...}``."""
    params = {"source": document.get("source") or "",
              "sliders": {s["name"]: s["default"] for s in document.get("sliders") or []}}
    result = harness(f"return H.transpileRaw({json.dumps(params)});", out_dir)
    if result.get("status") in {"not_ready", "timeout", "browser_error", "exception", "no_browser",
                                "busy"}:
        return {"error": result.get("detail") or result.get("status")}
    if not result.get("ok"):
        return {"error": result.get("log") or "the transpiler refused the shader"}
    return {"text": result.get("text") or ""}


def _write_pngs(result: dict, out_dir: Path, labels: tuple[tuple[str, str], ...]) -> list[str]:
    frames = []
    for key, label in labels:
        data = result.get(key)
        if isinstance(data, str) and data.startswith("data:image/png;base64,"):
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"proof-{label}.png"
            path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
            frames.append(str(path))
    return frames


def _not_proven(method: str, why: str, frames=None, **extra) -> dict:
    return {"method": method, "verdict": "not_proven", "why": why, "checks": [], "diff": None,
            "frames": frames or [], **extra}


PROOF_TIMES = (0.5, 1.0, 2.0)
PRIMARY_TIME = 1.0


def quarter_values(document: dict) -> dict[str, float]:
    """Every slider at a quarter of its range: a second set that exposes an unwired slider."""
    return {s["name"]: float(s["min"]) + 0.25 * (float(s["max"]) - float(s["min"]))
            for s in document.get("sliders") or []}


def proof_checks(document: dict, *, slider_sets: bool = True) -> list[dict]:
    """The moments and slider sets one proof compares.

    The first set is the effective values, and the export renders it on its own defaults, so the
    check also proves the export ships the tuned look. The second set passes each slider at a
    quarter of its range to the export. Default-only checks come first: a TSL module keeps a value
    once it was set.
    """
    effective = record.effective_values(document)
    checks = [{"t": t, "values": effective, "override": False, "primary": t == PRIMARY_TIME}
              for t in PROOF_TIMES]
    if slider_sets and document.get("sliders"):
        quarter = quarter_values(document)
        checks += [{"t": t, "values": quarter, "override": True, "primary": False}
                   for t in PROOF_TIMES]
    return checks


def _compare(document: dict, kind: str, module_source: str, out_dir: Path, *, method: str,
             factory: str | None = None, slider_sets: bool = True, export_label: str = "export",
             same: str, different: str) -> dict:
    checks = proof_checks(document, slider_sets=slider_sets)
    params = {"kind": kind, "source": document.get("source") or "", "moduleSource": module_source,
              "factory": factory, "width": 512, "height": 288, "checks": checks}
    result = harness(f"return H.compareChecks({json.dumps(params)});", out_dir / "proof")
    frames = _write_pngs(result, out_dir, (("reference_png", "reference"),
                                           ("export_png", export_label)))
    if result.get("status") == "not_ready":
        return _not_proven(method, "the harness did not load", frames)
    if result.get("status") in {"timeout", "browser_error", "exception", "no_browser", "busy"}:
        return _not_proven(method, str(result.get("detail") or result.get("error")
                                       or result.get("status"))[:800], frames,
                           stage=result.get("phase") or result.get("status"))
    if not result.get("ok"):
        where = f" at t = {result['t']:g} s" if isinstance(result.get("t"), (int, float)) else ""
        return _not_proven(method, ((result.get("log") or "the export failed to render")
                                    + where)[:800], frames, stage=result.get("stage"))
    measured = result.get("checks") or []
    if len(measured) != len(checks):
        return _not_proven(method, "the harness returned fewer checks than it was asked for", frames)
    rows = [{"t": check["t"], "values": check["values"], "mean": got.get("mean"),
             "p99": got.get("p99")} for check, got in zip(checks, measured)]

    def passes(row: dict) -> bool:
        return (isinstance(row["mean"], (int, float)) and isinstance(row["p99"], (int, float))
                and row["mean"] <= PROOF_MEAN_LIMIT and row["p99"] <= PROOF_P99_LIMIT)

    worst = {"mean": max(row["mean"] for row in rows), "p99": max(row["p99"] for row in rows)}
    failing = [row for row in rows if not passes(row)]
    moments = ", ".join(f"{t:g}" for t in PROOF_TIMES)
    sets = ("the effective slider values and every slider at a quarter of its range"
            if slider_sets and document.get("sliders") else "the effective slider values")
    if not failing:
        why = (f"{same} at t = {moments} s with {sets}, within {PROOF_MEAN_LIMIT:g}/255 mean and "
               f"{PROOF_P99_LIMIT:g}/255 p99 (worst {worst['mean']:g} mean, {worst['p99']:g} p99)")
    else:
        first = failing[0]
        which = "the quarter-range slider values" if first["values"] != record.effective_values(document) \
            else "the effective slider values"
        why = (f"{different}: {len(failing)} of {len(rows)} checks fail; first at t = {first['t']:g} s "
               f"with {which} ({first['mean']:g}/255 mean, {first['p99']:g}/255 p99)")
    proof = {"method": method, "verdict": "differs" if failing else "match", "why": why,
             "checks": rows, "diff": worst, "frames": frames}
    if isinstance(result.get("grid"), list):
        proof["grid"] = result["grid"]
    if result.get("slot_wait_ms"):
        proof["slot_wait_ms"] = result["slot_wait_ms"]
    return proof


def prove_three(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    """Render the source and the exported material factory in the offline harness and compare."""
    name = pascal(document.get("name"))
    module_source = files.get(f"{name}Material.js")
    if module_source is None:
        return _not_proven("three-harness", "this target has no material module to render")
    return _compare(document, "material", module_source, out_dir, method="three-harness",
                    factory=f"create{name}Material", same="the material renders like the source",
                    different="the material does not render like the source")


def prove_tsl(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    name = pascal(document.get("name"))
    module_source = files.get(f"{name}.tsl.js")
    if module_source is None:
        return _not_proven("tsl-harness", "the export has no module to render")
    return _compare(document, "tsl", module_source, out_dir, method="tsl-harness",
                    export_label="tsl", same="the transpiled module renders like the source",
                    different="the transpiled module does not render like the source")


def prove_shadertoy(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    """Render the exported Image tab through the same GLSL path as the source and compare."""
    name = pascal(document.get("name"))
    tab = files.get(f"{name}.shadertoy.glsl")
    if tab is None:
        return _not_proven("shadertoy-harness", "the export has no Image tab to render")
    return _compare(document, "glsl", tab, out_dir, method="shadertoy-harness",
                    slider_sets=False, export_label="shadertoy",
                    same="the Image tab (sliders as const floats) renders like the source",
                    different="the Image tab does not render like the source")


def _prove_shim(document: dict, files: dict[str, str], out_dir: Path, *, method: str, kind: str,
                filename: str, host: str) -> dict:
    """Render the exported file inside a WebGL2 shim of `host`'s wrapper and compare."""
    text = files.get(filename)
    if text is None:
        return _not_proven(method, f"the export has no {filename} to render")
    if document.get("channels"):
        return _not_proven(method, "the shader reads channel inputs and the reference renderer has "
                                   "none, so the image input mapping is not proven")
    proven = _compare(document, kind, text, out_dir, method=method, export_label=kind,
                      same=f"the file renders like the source in a WebGL2 shim of {host}",
                      different=f"the file does not render like the source in a WebGL2 shim of {host}")
    if proven["verdict"] == "match":
        proven["why"] += f"; {host} itself has not rendered it yet (the target is experimental)"
    return proven


def prove_td(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    return _prove_shim(document, files, out_dir, method="td-shim", kind="td",
                       filename=f"{pascal(document.get('name'))}.td.glsl",
                       host="TouchDesigner's GLSL TOP")


def prove_isf(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    return _prove_shim(document, files, out_dir, method="isf-shim", kind="isf",
                       filename=f"{pascal(document.get('name'))}.fs", host="an ISF host")


def embedded_module(handoff_text: str, filename: str) -> str | None:
    """The body of the fenced block under ``### <filename>``, read the way CommonMark reads it."""
    lines = handoff_text.split("\n")
    try:
        start = lines.index(f"### {filename}") + 1
    except ValueError:
        return None
    if start >= len(lines):
        return None
    fence = lines[start]
    ticks = len(fence) - len(fence.lstrip("`"))
    if ticks < 3:
        return None
    body = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped and set(stripped) == {"`"} and len(stripped) >= ticks:
            return "\n".join(body) + "\n"
        body.append(line)
    return None


def prove_agent(document: dict, files: dict[str, str], out_dir: Path) -> dict:
    """The handoff carries the three material inline: prove that embedded module itself."""
    name = pascal(document.get("name"))
    method = "three-harness (embedded module)"
    text = files.get(f"{name}.handoff.md")
    module_source = embedded_module(text or "", f"{name}Material.js")
    if module_source is None:
        return _not_proven(method, "the handoff has no embedded material module to render")
    proven = prove_three(document, {f"{name}Material.js": module_source}, out_dir)
    proven["method"] = method
    proven["why"] = "the handoff's embedded three material: " + (proven.get("why") or "")
    return proven


def prove(document: dict, target_row, files: dict[str, str], out_dir: Path) -> dict:
    """The ProofResult for one target's files."""
    if target_row.proof == "shadertoy-harness":
        return prove_shadertoy(document, files, out_dir)
    if target_row.proof == "embedded-three":
        return prove_agent(document, files, out_dir)
    if target_row.proof == "tsl-harness":
        return prove_tsl(document, files, out_dir)
    if target_row.proof == "td-shim":
        return prove_td(document, files, out_dir)
    if target_row.proof == "isf-shim":
        return prove_isf(document, files, out_dir)
    if target_row.proof == "three-harness":
        proven = prove_three(document, files, out_dir)
        if target_row.id == "r3f":
            proven["why"] = (proven.get("why") or "") + \
                "; the .tsx wrapper itself is proven by the app render (E2), not by the harness"
        return proven
    return _not_proven(target_row.proof, f"no proof runner for {target_row.proof}")


# --- the stored proof ----------------------------------------------------------------------------

def export_dir(base: Path, shader_id: str, version: int, target: str) -> Path:
    if target not in registry.EXPORT_TARGETS:
        raise ValueError(f"unknown target {target!r}")
    return record.version_dir(base, shader_id, version) / "exports" / target


def read_proof(base: Path, shader_id: str, version: int, target: str) -> dict | None:
    """The ProofResult stored by the last export of this target, or None."""
    if not record.valid_id(shader_id) or target not in registry.EXPORT_TARGETS:
        return None
    path = export_dir(base, shader_id, version, target) / PROOF_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- the export ----------------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snippet(target: str, name: str) -> str:
    return {
        "three": f"import {{ create{name}Material }} from './{name}Material.js'\n"
                 f"const material = create{name}Material()\n"
                 "// material.setSize(width, height); material.update(elapsedSeconds)",
        "r3f": f"import {{ {name} }} from './{name}'\n\n<{name} />",
        "shadertoy": "Paste into shadertoy.com/new, Image tab.",
        "td": f"GLSL TOP > docked Text DAT: paste {name}.td.glsl; Vectors page: iTime = absTime.seconds",
        "isf": f"cp {name}.fs ~/Library/Graphics/ISF/   # VDMX; in Wire: ISF node > Fragment Shader",
    }.get(target, "")


def _failure(error: str, **extra) -> dict:
    return {"success": False, "error": error, **extra}


REFERENCE_PNG = "reference-t1.png"


def _finish_handoff(document: dict, target_row, files: dict[str, str], options: dict,
                    proof: dict, out_dir: Path) -> list[str]:
    """Write the measured acceptance check into the handoff and ship its reference frame.

    The handoff is rebuilt with the reference grid the proof measured. Its embedded module must
    stay the module the proof rendered; if it does not, the proof no longer describes the file.
    """
    reference = out_dir / "proof-reference.png"
    stale = out_dir / REFERENCE_PNG
    if stale.exists():
        stale.unlink()
    if not isinstance(proof.get("grid"), list) or not reference.is_file():
        return list(files)
    name = pascal(document.get("name"))
    final = target_row.files(document, {**options, "acceptance": {"grid": proof["grid"],
                                                                  "png": REFERENCE_PNG}})
    key = f"{name}.handoff.md"
    if embedded_module(final[key], f"{name}Material.js") != embedded_module(files[key], f"{name}Material.js"):
        proof.update(verdict="not_proven", why="the rebuilt handoff embeds a different module "
                                                "than the one the proof rendered")
        return list(files)
    for filename, text in final.items():
        (out_dir / filename).write_text(text, encoding="utf-8")
    shutil.copyfile(reference, stale)
    return [*final, REFERENCE_PNG]


def export_view(document: dict, meta: dict | None, settings: dict | None = None) -> dict:
    """The version as every exporter sees it: the shader's current name (a rename applies to the
    next export; the record keeps the name it was made with) and, for the user's own shaders, the
    author from the settings. The licence stays the version's own stamp (constraint 8)."""
    view = dict(document)
    if meta and meta.get("name"):
        view["name"] = meta["name"]
    provenance = dict(view.get("provenance") or {})
    settings = settings or {}
    if not provenance.get("author") and provenance.get("origin") != "import" and settings.get("author_name"):
        author = settings["author_name"]
        if settings.get("author_url"):
            author += f" ({settings['author_url']})"
        provenance["author"] = author
    view["provenance"] = provenance
    return view


def export_shader(conn: sqlite3.Connection, base: Path, shader_id: str, target: str, *,
                  version: int | None = None, dest_dir: str | None = None, overwrite: bool = False,
                  accept_unproven: bool = False, settings: dict | None = None,
                  unattended_reason: str | None = None) -> dict:
    """Write, prove, store the proof, then copy. Returns the `shader_export` payload.

    `settings` defaults to the profile's settings read now (`settings.read_settings`), so a change
    in the settings form applies to the next export. Chromium children count against this
    profile's slots under `<base>/locks`.
    """
    if settings is None:
        settings = settings_mod.read_settings()
    with renderer.chromium_slots(Path(base) / "locks"):
        payload = _export(conn, base, shader_id, target, version=version, dest_dir=dest_dir,
                          overwrite=overwrite, accept_unproven=accept_unproven, settings=settings,
                          unattended_reason=unattended_reason)
    waited = (payload.get("proof") or {}).get("slot_wait_ms") or 0
    if payload.get("success") and waited >= 1000:
        payload["next"] = (f"This export waited {waited / 1000:.0f} s for a free renderer (at most "
                           f"{renderer.MAX_CHILDREN} run at once per profile). " + payload.get("next", ""))
    return payload


def _export(conn: sqlite3.Connection, base: Path, shader_id: str, target: str, *,
            version: int | None, dest_dir: str | None, overwrite: bool, accept_unproven: bool,
            settings: dict, unattended_reason: str | None) -> dict:
    shader_id = str(shader_id or "").strip()
    if not shader_id:
        return _failure("id is required")
    if not record.valid_id(shader_id):
        return _failure(f"invalid id {shader_id!r}: ids are lowercase letters, digits and '-' "
                        "(take one from shader_library list)")
    target_row = registry.EXPORT_TARGETS.get(target)
    if target_row is None:
        return _failure(f"unknown target {target!r}", targets=registry.target_ids())
    document = (record.get_version(conn, shader_id, int(version)) if version is not None
                else record.latest(conn, shader_id))
    if document is None:
        return _failure(f"no shader {shader_id}" + (f" version {version}" if version is not None else "")
                        + " in the library")
    document = export_view(document, record.get_meta(conn, shader_id), settings)
    number: int = int(document["version"])
    name = pascal(document.get("name"))
    base_payload = {"id": shader_id, "version": number, "target": target}

    # A bad dest_dir is refused before anything is rendered: the proof is the slow part.
    if dest_dir:
        planned = list(target_row.files(document, {"credit": True, "raw": ""})) \
            if target_row.proof != "tsl-harness" else [target_row.main_file.format(name=name)]
        if target_row.proof == "embedded-three":
            planned.append(REFERENCE_PNG)
        decision = dest_decision(dest_dir, planned, overwrite=overwrite,
                                 allow_writes=bool(settings.get("allow_project_writes", False)),
                                 reason=unattended_reason)
        if not decision["ok"]:
            return _failure(decision["error"], **base_payload,
                            next="Fix dest_dir, or export without it and copy the files yourself.")

    out_dir = export_dir(base, shader_id, number, target)
    out_dir.mkdir(parents=True, exist_ok=True)
    options = {"credit": bool(settings.get("credit_line", True))}
    if target_row.proof == "tsl-harness":
        refusal = tsl.guard(document)
        if refusal:
            return _failure(f"tsl_not_supported: {refusal}", **base_payload,
                            proof=_not_proven("tsl-harness", refusal))
        raw = raw_transpile(document, out_dir / "build")
        if raw.get("error"):
            return _failure(f"transpiler: {raw['error']}", **base_payload,
                            proof=_not_proven("tsl-harness", raw["error"]))
        options["raw"] = raw["text"]
    files = target_row.files(document, options)
    for filename, text in files.items():
        (out_dir / filename).write_text(text, encoding="utf-8")
    outputs = list(files)

    proof = prove(document, target_row, files, out_dir)
    if target_row.proof == "embedded-three":
        outputs = _finish_handoff(document, target_row, files, options, proof, out_dir)
    (out_dir / PROOF_FILE).write_text(json.dumps(proof, indent=2), encoding="utf-8")

    written = []
    for filename in outputs:
        data = (out_dir / filename).read_bytes()
        written.append({"path": str(out_dir / filename), "sha256": _sha256(data), "bytes": len(data)})

    payload = {
        "success": True,
        **base_payload,
        "label": target_row.label,
        "main_file": target_row.main_file.format(name=name),
        "licence": licence_text(document),
        "files": written,
        "copied_to": [],
        "steps": target_row.steps(document, dest_dir),
        "snippet": _snippet(target, name),
        "proof": proof,
    }
    verdict = proof.get("verdict")
    if dest_dir:
        if verdict == "match" or accept_unproven:
            # Checked again: the folder may have changed while the proof ran.
            decision = dest_decision(dest_dir, outputs, overwrite=overwrite,
                                     allow_writes=bool(settings.get("allow_project_writes", False)),
                                     reason=unattended_reason)
            if not decision["ok"]:
                return _failure(decision["error"], **base_payload, files=written, proof=proof,
                                next="Fix dest_dir, or copy the files from the exports folder yourself.")
            for filename in outputs:
                target_path = Path(str(dest_dir)) / filename
                shutil.copyfile(out_dir / filename, target_path)
                payload["copied_to"].append(str(target_path))
            if verdict != "match":
                payload["copied_unproven"] = True
                payload["warning"] = (f"copied into dest_dir although the proof says {verdict}: "
                                      f"{proof.get('why')}")
        else:
            payload["copy_skipped"] = (f"dest_dir was not written because the proof says {verdict}. "
                                       "Pass accept_unproven=true to copy anyway, and tell the user.")
    if verdict == "match":
        payload["next"] = ("The export renders like the source. Put the steps and the snippet in "
                           "your reply; do not rewrite the shader maths.")
    else:
        payload["next"] = ("Show me the files and the proof verdict. Where the proof says `differs` "
                           "or `not_proven`, say why instead of claiming the export works.")
    return payload
