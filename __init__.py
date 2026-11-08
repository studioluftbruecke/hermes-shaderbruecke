"""shaderbruecke: the agent's render-see-fix loop for GLSL shaders."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from pathlib import Path

from . import checks, record, renderer, unattended
from .exporters import registry, tsl
from .exporters.header import pascal

PLUGIN = "shaderbruecke"
MAX_SOURCE_BYTES = 64 * 1024

SHADER_RENDER = {
    "name": "shader_render",
    "description": (
        "Compile, render and check a Shadertoy-style GLSL ES 3.00 fragment shader on the Hermes "
        "host. Write only the body: `void mainImage(out vec4 fragColor, in vec2 fragCoord)` plus "
        "helper functions. The uniforms iResolution (vec3), iTime, iTimeDelta, iFrame, iMouse and "
        "iDate are injected; do not declare #version, precision, uniforms or main(). Use constant "
        "loop bounds. Tunable numbers become sliders: a line "
        "`// @slider name 0.0..1.0 = 0.5 step 0.01` declares a uniform and a Desktop slider. "
        "Before the browser starts, a lint checks for the mistakes that hang a GPU or fail to "
        "compile (`while (true)`, webgl1 names, redeclared uniforms, a second main()). Then it "
        "renders frames at t = 0, 1 and 2.5 s, builds a contact sheet, measures whether the frames "
        "are flat or black, counts NaN pixels and scores the flash risk. After a successful "
        "render: call vision_analyze on `contact_sheet` and look at the frames before you "
        "describe the result; then put the `media` line on its own line in your reply so Hermes "
        "Desktop plays the shader live. A `MEDIA:` line is only returned for a render that is not "
        "flat and not flash-blocked. Pass `id` to keep a new version of an existing shader and "
        "`parent_id` to fork one; versions are kept and listed by shader_library."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Full shader source defining mainImage (Shadertoy conventions).",
            },
            "name": {
                "type": "string",
                "description": "Short title for the shader, e.g. 'golden-hour sky'.",
            },
            "time": {
                "type": "number",
                "description": "Value of iTime (seconds) for the snapshot frame. Default 1.0.",
            },
            "id": {
                "type": "string",
                "description": "Id of an existing shader: this render becomes its next version.",
            },
            "parent_id": {
                "type": "string",
                "description": "Id of a shader to fork: a new id is created with lineage to it.",
            },
        },
        "required": ["source"],
    },
}

SHADER_LIBRARY = {
    "name": "shader_library",
    "description": (
        "The shaders this profile has rendered. `list` returns the newest 20 (id, name, version, "
        "status, thumbnail). `get` returns one full record: source, slider specs, the human's "
        "latest slider values, checks and provenance. `fork` copies a shader into a new id and "
        "records the lineage, so you can change it without losing the original."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "fork"]},
            "id": {"type": "string", "description": "Shader id for get and fork."},
            "version": {"type": "integer", "description": "Version for get (default: latest)."},
            "name": {"type": "string", "description": "Title for the fork."},
            "limit": {"type": "integer", "description": "How many rows `list` returns (max 50)."},
        },
        "required": ["action"],
    },
}


SHADER_EXPORT = {
    "name": "shader_export",
    "description": (
        "Write one export target's files for a shader in the library, then prove them. Targets: "
        "`three` (a ShaderMaterial factory module plus the standalone fragment), `tsl` (a node "
        "module made with three r187's own transpiler), `r3f` (a React Three Fiber component using "
        "that factory), `shadertoy` (a paste-ready Image tab) and `agent` (one Markdown handoff "
        "block for another agent or a teammate). Every "
        "file starts with a self-describing header: name, id@version, target, the uniform contract, "
        "the sliders with their ranges, provenance and licence. The three and r3f exports are "
        "rendered in an offline three r187 harness and compared frame by frame with the source: "
        "the result says `match`, `differs` or `not_proven` and why, never a silent pass. Files "
        "always land in the shader's own exports folder; pass `dest_dir` (an existing absolute "
        "directory) to also copy them into a project, which never overwrites unless "
        "`overwrite=true`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Shader id from shader_library."},
            "target": {"type": "string", "enum": registry.target_ids()},
            "version": {"type": "integer", "description": "Version to export (default: latest)."},
            "dest_dir": {"type": "string",
                         "description": "Existing absolute directory to copy the files into."},
            "overwrite": {"type": "boolean",
                          "description": "Allow replacing files in dest_dir. Default false."},
        },
        "required": ["id", "target"],
    },
}

PROOF_MEAN_LIMIT = 2.0     # 2/255
PROOF_P99_LIMIT = 8.0      # 8/255


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "shader"


def _numbered(source: str, lines: set[int]) -> str:
    """The source lines named by errors, with one line of context, numbered like the log."""
    src = source.splitlines()
    keep = sorted({n for line in lines for n in (line - 1, line, line + 1) if 1 <= n <= len(src)})
    return "\n".join(f"{'>' if n in lines else ' '}{n:4d} | {src[n - 1]}" for n in keep)


def _storage():
    """The profile's database and data directory. Called inside handlers, never at import."""
    from plugins.plugin_storage import plugin_data_dir, plugin_db
    return plugin_db(PLUGIN), plugin_data_dir(PLUGIN)


def _check_report(bundle: renderer.BundleResult, flash: dict, lint: list[dict]) -> dict:
    return {
        "status": bundle.status,
        "diagnostics": [{"severity": e.severity, "where": e.where, "line": e.line,
                         "message": e.message} for e in bundle.errors],
        "lint": lint,
        "frames": bundle.frames,
        "contact_sheet": bundle.contact_sheet,
        "flat": checks.flat(bundle.frames),
        "flash": flash,
        "wall_ms": bundle.wall_ms,
        "draw_ms": bundle.draw_ms,
        "renderer": bundle.renderer,
    }


def _record_document(shader_id: str, version: int, name: str, source: str, sliders: list[dict],
                     parent_id: str | None, report: dict, frag: Path, bundle: renderer.BundleResult) -> dict:
    return {
        "id": shader_id,
        "version": version,
        "name": name,
        "source": source,
        "sliders": sliders,
        "slider_values": record.slider_values(sliders),
        "channels": [],
        "provenance": {"origin": "agent", "url": None, "author": None, "licence": None,
                       "parent": parent_id},
        "checks": report,
        "edited_by": "agent",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {"frag": str(frag), "contact_sheet": bundle.contact_sheet,
                  "snapshot": next((f.get("png_path") for f in bundle.frames), None)},
    }


def shader_render(args: dict, **kwargs) -> str:
    source = args.get("source")
    if not isinstance(source, str) or not source.strip():
        return json.dumps({"success": False, "error": "source is required"})
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return json.dumps({"success": False, "error": f"source exceeds {MAX_SOURCE_BYTES} bytes"})
    name = str(args.get("name") or "shader").strip()[:80]
    try:
        t = float(args.get("time", 1.0))
    except (TypeError, ValueError):
        t = 1.0

    conn, base = _storage()
    parent_id = args.get("parent_id")
    parent_id = str(parent_id) if parent_id else None
    if parent_id and record.latest(conn, parent_id) is None:
        return json.dumps({"success": False, "error": f"no shader with id {parent_id} to fork"})
    shader_id = str(args.get("id") or "").strip() or record.new_id(name)
    if parent_id:
        shader_id = record.new_id(name)
    version = record.next_version(conn, shader_id)
    out_dir = record.version_dir(base, shader_id, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    frag = out_dir / "shader.frag"
    frag.write_text(source, encoding="utf-8")

    sliders = record.parse_sliders(source)
    findings = checks.lint(source)
    lint_payload = [f.as_dict() for f in findings]
    lint_errors = [f for f in findings if f.severity == "error"]
    if lint_errors:
        payload = {
            "success": False,
            "error": f"lint_error: {len(lint_errors)} error(s), {len(findings) - len(lint_errors)} warning(s)",
            "id": shader_id,
            "version": version,
            "lint": lint_payload,
            "next": ("Fix every lint error and call shader_render again with the full corrected source. "
                     + "; ".join(f"line {f.line}: {f.hint}" for f in lint_errors[:4])),
        }
        (out_dir / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return json.dumps(payload)

    times = tuple(sorted({0.0, max(0.0, t), 2.5}))
    bundle = renderer.render_bundle(source, out_dir, times=times, sliders=sliders)
    flash = checks.flash_verdict(bundle.flash.get("lumas", []), bundle.flash.get("reds", []),
                                 fps=bundle.flash.get("fps", 60.0)) if bundle.flash else \
        {"max_general_per_s": 0, "red": False, "verdict": "none", "pairs": 0, "frames": 0}
    (out_dir / "result.json").write_text(json.dumps(bundle.as_dict(), indent=2), encoding="utf-8")

    if bundle.status == "compile_error" or bundle.status == "link_error":
        shader_errors = [e for e in bundle.errors if e.where == "shader"]
        return json.dumps({
            "success": False,
            "error": f"{bundle.status.replace('_', ' ')}: {len(bundle.errors)} diagnostic(s)",
            "id": shader_id,
            "version": version,
            "compile_log": bundle.compile_log,
            "errors": [{"line": e.line, "where": e.where, "message": e.message} for e in bundle.errors],
            "source_excerpt": _numbered(source, {e.line for e in shader_errors}),
            "lint": lint_payload,
            "next": "Fix these lines and call shader_render again with the full corrected source.",
        })

    if bundle.status != "ok":
        return json.dumps({"success": False, "error": bundle.detail or bundle.status,
                           "status": bundle.status, "id": shader_id, "version": version,
                           "lint": lint_payload})

    is_flat = checks.flat(bundle.frames)
    blocked = flash.get("verdict") == "block"
    report = _check_report(bundle, flash, lint_payload)
    record.save(conn, _record_document(shader_id, version, name, source, sliders, parent_id,
                                       report, frag, bundle))
    snapshot_frame = next((f for f in bundle.frames if abs(f["t"] - t) < 1e-6), bundle.frames[-1])
    payload = {
        "success": True,
        "id": shader_id,
        "version": version,
        "name": name,
        "frag_path": str(frag),
        "png_path": snapshot_frame.get("png_path"),
        "compile_log": bundle.compile_log,
        "snapshot": {"time": snapshot_frame["t"], "width": 512, "height": 288,
                     "draw_ms": snapshot_frame.get("draw_ms"),
                     "luma_mean": snapshot_frame.get("luma_mean"),
                     "luma_std": snapshot_frame.get("luma_std")},
        "renderer": bundle.renderer,
        "sliders": sliders,
        "checks": report,
        "contact_sheet": bundle.contact_sheet,
    }
    if parent_id:
        payload["parent_id"] = parent_id
    if is_flat:
        payload["warning"] = "the frames are a flat single colour or black; the shader is probably wrong"
        payload["next"] = ("The frames are black or flat. Check that fragColor is written in every path "
                           "and that alpha is 1.0, then call shader_render again.")
    elif blocked:
        payload["warning"] = (f"flash risk: {flash['max_general_per_s']} general flashes per second; "
                              "no autoplay media line was returned")
        payload["next"] = ("The shader flashes too fast (more than 3 general flashes per second). "
                           "Slow the animation or lower the contrast between states, then render again.")
    else:
        payload["media"] = f"MEDIA:{frag}"
        payload["next"] = ("Call vision_analyze on contact_sheet and look at the three frames before you "
                           "describe the result; then put the `media` line on its own line in your reply so "
                           "Desktop plays the shader live.")
    if flash.get("verdict") == "warn" and not is_flat:
        payload.setdefault("warning", "flash risk: 3 general flashes per second (WCAG warn threshold)")
    return json.dumps(payload)


def shader_library(args: dict, **kwargs) -> str:
    action = str(args.get("action") or "list").lower()
    conn, base = _storage()
    if action == "list":
        limit = args.get("limit")
        limit = max(1, min(50, int(limit))) if isinstance(limit, (int, float)) else 20
        rows = [{"id": r["id"], "name": r.get("name"), "version": r.get("version"),
                 "status": (r.get("checks") or {}).get("status"),
                 "parent": (r.get("provenance") or {}).get("parent"),
                 "thumbnail": (r.get("files") or {}).get("contact_sheet"),
                 "created_at": r.get("created_at")}
                for r in record.list_recent(conn, limit)]
        return json.dumps({"success": True, "count": len(rows), "shaders": rows})

    shader_id = str(args.get("id") or "").strip()
    if not shader_id:
        return json.dumps({"success": False, "error": "id is required for get and fork"})

    if action == "get":
        version = args.get("version")
        if isinstance(version, (int, float)):
            found = record.get_version(conn, shader_id, int(version))
        else:
            found = record.latest(conn, shader_id)
        if found is None:
            return json.dumps({"success": False, "error": f"no shader {shader_id}"
                                                          + (f" version {version}" if version else "")})
        return json.dumps({"success": True, "shader": found})

    if action == "fork":
        forked = record.fork(conn, base, shader_id, name=args.get("name"))
        if forked is None:
            return json.dumps({"success": False, "error": f"no shader {shader_id} to fork"})
        return json.dumps({"success": True, "shader": forked, "parent_id": shader_id})

    return json.dumps({"success": False, "error": f"unknown action {action}"})


def _prove_three(record_document: dict, files: dict[str, str], out_dir: Path) -> dict:
    """Render the source and the exported material factory in the offline harness and compare."""
    name = pascal(record_document.get("name"))
    module_key = f"{name}Material.js"
    module_source = files.get(module_key)
    if module_source is None:
        some = next((key for key in files if key.endswith(("Material.js", ".tsx"))), None)
        if some is None:
            return {"method": "three-harness", "diff": None, "frames": [], "verdict": "not_proven",
                    "why": "this target has no material module to render"}
        module_key = some
    sliders = {s["name"]: s["default"] for s in record_document.get("sliders") or []}
    params = {"source": record_document.get("source") or "", "moduleSource": module_source,
              "factory": f"create{name}Material", "sliders": sliders,
              "width": 512, "height": 288, "time": 1.0}
    import base64
    raw_expression = ("(async () => { const H = window.sbHarness;"
                      " if (!H || !H.ready) return {status: 'not_ready'};"
                      f" const r = await H.compareExport({json.dumps(params)}); return r; }})()")
    result = renderer.run_page(Path(__file__).with_name("headless") / "harness.html", raw_expression,
                               timeout_s=90.0, out_dir=out_dir / "proof")
    frames = []
    for key, label in (("reference_png", "reference"), ("export_png", "export")):
        data = result.get(key)
        if isinstance(data, str) and data.startswith("data:image/png;base64,"):
            path = out_dir / f"proof-{label}.png"
            path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
            frames.append(str(path))
    if result.get("status") == "not_ready":
        return {"method": "three-harness", "diff": None, "frames": frames, "verdict": "not_proven",
                "why": "the harness did not load"}
    if not result.get("ok"):
        return {"method": "three-harness", "diff": None, "frames": frames, "verdict": "not_proven",
                "why": (result.get("log") or result.get("detail") or "the exported module failed to render")[:600],
                "stage": result.get("stage")}
    diff = result.get("diff") or {}
    matched = diff.get("mean", 999) <= PROOF_MEAN_LIMIT and diff.get("p99", 999) <= PROOF_P99_LIMIT
    return {"method": "three-harness", "diff": {"mean": diff.get("mean"), "p99": diff.get("p99")},
            "frames": frames, "verdict": "match" if matched else "differs",
            "why": (f"reference and export agree within {PROOF_MEAN_LIMIT:g}/255 mean and "
                    f"{PROOF_P99_LIMIT:g}/255 p99") if matched else
                   (f"frames differ by {diff.get('mean')}/255 mean, {diff.get('p99')}/255 p99; "
                    "the export does not render like the source")}


HARNESS = Path(__file__).with_name("headless") / "harness.html"


def _harness(expression: str, out_dir: Path, timeout_s: float = 90.0) -> dict:
    return renderer.run_page(HARNESS, f"(async () => {{ const H = window.sbHarness;"
                                      " if (!H || !H.ready) return {status: 'not_ready'};"
                                      f" return await (async () => {{ {expression} }})(); }})()",
                             timeout_s=timeout_s, out_dir=out_dir)


def _raw_transpile(document: dict, out_dir: Path) -> dict:
    params = {"source": document.get("source") or "",
              "sliders": {s["name"]: s["default"] for s in document.get("sliders") or []}}
    result = _harness(f"return H.transpileRaw({json.dumps(params)});", out_dir)
    if result.get("status") in {"not_ready", "timeout", "browser_error", "exception"}:
        return {"error": result.get("detail") or result.get("status")}
    if not result.get("ok"):
        return {"error": result.get("log") or "the transpiler refused the shader"}
    return {"text": result.get("text") or ""}


def _prove_tsl(record_document: dict, files: dict[str, str], out_dir: Path) -> dict:
    import base64
    name = pascal(record_document.get("name"))
    module_source = files.get(f"{name}.tsl.js")
    if module_source is None:
        return {"method": "tsl-harness", "diff": None, "frames": [], "verdict": "not_proven",
                "why": "the export has no module to render"}
    params = {"source": record_document.get("source") or "", "moduleSource": module_source,
              "sliders": {s["name"]: s["default"] for s in record_document.get("sliders") or []},
              "width": 512, "height": 288, "time": 1.0}
    result = _harness(f"return H.compareModules({json.dumps(params)});", out_dir)
    frames = []
    for key, label in (("reference_png", "reference"), ("tsl_png", "tsl")):
        data = result.get(key)
        if isinstance(data, str) and data.startswith("data:image/png;base64,"):
            path = out_dir / f"proof-{label}.png"
            path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
            frames.append(str(path))
    if result.get("status") == "not_ready":
        return {"method": "tsl-harness", "diff": None, "frames": frames, "verdict": "not_proven",
                "why": "the harness did not load"}
    if not result.get("ok"):
        return {"method": "tsl-harness", "diff": None, "frames": frames, "verdict": "not_proven",
                "why": (result.get("log") or result.get("detail") or "the module failed to render")[:800],
                "stage": result.get("stage")}
    diff = result.get("diff") or {}
    matched = diff.get("mean", 999) <= PROOF_MEAN_LIMIT and diff.get("p99", 999) <= PROOF_P99_LIMIT
    return {"method": "tsl-harness", "diff": {"mean": diff.get("mean"), "p99": diff.get("p99")},
            "frames": frames, "verdict": "match" if matched else "differs",
            "why": (f"the transpiled module renders within {PROOF_MEAN_LIMIT:g}/255 mean and "
                    f"{PROOF_P99_LIMIT:g}/255 p99 of the source") if matched else
                   (f"the module differs by {diff.get('mean')}/255 mean, {diff.get('p99')}/255 p99")}


def _proof(record_document: dict, target_row, files: dict[str, str], out_dir: Path) -> dict:
    if target_row.proof == "identity":
        return {"method": "identity", "diff": None, "frames": [], "verdict": "match",
                "why": "the record's source is already a Shadertoy Image tab; the sliders became const floats"}
    if target_row.proof == "tsl-harness":
        return _prove_tsl(record_document, files, out_dir)
    if target_row.proof == "three-harness":
        proven = _prove_three(record_document, files, out_dir)
        if target_row.id == "r3f":
            proven["why"] = (proven.get("why") or "") + \
                "; the .tsx wrapper itself is proven by the app render (E2), not by the harness"
        return proven
    return {"method": target_row.proof, "diff": None, "frames": [], "verdict": "not_proven",
            "why": f"no proof runner for {target_row.proof}"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def shader_export(args: dict, *, config: dict | None = None, **kwargs) -> str:
    config = config or {}
    shader_id = str(args.get("id") or "").strip()
    target = str(args.get("target") or "").strip()
    if not shader_id:
        return json.dumps({"success": False, "error": "id is required"})
    target_row = registry.EXPORT_TARGETS.get(target)
    if target_row is None:
        return json.dumps({"success": False, "error": f"unknown target {target!r}",
                           "targets": registry.target_ids()})
    conn, base = _storage()
    version_arg = args.get("version")
    if isinstance(version_arg, (int, float)):
        document = record.get_version(conn, shader_id, int(version_arg))
    else:
        document = record.latest(conn, shader_id)
    if document is None:
        return json.dumps({"success": False, "error": f"no shader {shader_id} in the library"})

    out_dir = record.version_dir(base, shader_id, document["version"]) / "exports" / target
    out_dir.mkdir(parents=True, exist_ok=True)
    options = {"credit": bool(config.get("credit_line", True))}
    if target_row.proof == "tsl-harness":
        refusal = tsl.guard(document)
        if refusal:
            return json.dumps({"success": False, "id": shader_id, "version": document["version"],
                               "target": target, "error": f"tsl_not_supported: {refusal}",
                               "proof": {"method": "tsl-harness", "diff": None, "frames": [],
                                         "verdict": "not_proven", "why": refusal}})
        raw = _raw_transpile(document, out_dir / "build")
        if raw.get("error"):
            return json.dumps({"success": False, "id": shader_id, "version": document["version"],
                               "target": target, "error": f"transpiler: {raw['error']}",
                               "proof": {"method": "tsl-harness", "diff": None, "frames": [],
                                         "verdict": "not_proven", "why": raw["error"]}})
        options["raw"] = raw["text"]
    files = target_row.files(document, options)
    written = []
    for filename, text in files.items():
        path = out_dir / filename
        path.write_text(text, encoding="utf-8")
        written.append({"path": str(path), "sha256": _sha256(text), "bytes": len(text.encode("utf-8"))})

    dest = args.get("dest_dir")
    copied: list[str] = []
    if dest:
        decision = dest_decision(dest, list(files), overwrite=bool(args.get("overwrite")),
                                 allow_writes=bool(config.get("allow_project_writes", False)),
                                 reason=unattended.unattended_reason())
        if not decision["ok"]:
            return json.dumps({"success": False, "error": decision["error"], "files": written,
                               "next": "Fix dest_dir, or export without it and copy the files yourself."})
        dest_path = Path(str(dest))
        for filename, text in files.items():
            target_path = dest_path / filename
            target_path.write_text(text, encoding="utf-8")
            copied.append(str(target_path))

    proof = _proof(document, target_row, files, out_dir)
    name = pascal(document.get("name"))
    snippet = {
        "three": f"import {{ create{name}Material }} from './{name}Material.js'\n"
                 f"const material = create{name}Material()\n"
                 "// material.setSize(width, height); material.update(elapsedSeconds)",
        "r3f": f"import {{ {name} }} from './{name}'\n\n<{name} />",
        "shadertoy": "Paste into shadertoy.com/new, Image tab.",
    }.get(target, "")
    payload = {
        "success": True,
        "id": shader_id,
        "version": document["version"],
        "target": target,
        "label": target_row.label,
        "main_file": target_row.main_file.format(name=name),
        "files": written,
        "copied_to": copied,
        "steps": target_row.steps(document, dest),
        "snippet": snippet,
        "proof": proof,
        "next": ("Show me the files and the proof verdict. Where the proof says `differs` or "
                 "`not_proven`, say why instead of claiming the export works.")
        if proof.get("verdict") != "match" else
        ("The export renders like the source. Put the steps and the snippet in your reply; do not "
         "rewrite the shader maths."),
    }
    return json.dumps(payload)


def register(ctx) -> None:
    ctx.register_tool(
        name="shader_render",
        toolset="shaderbruecke",
        schema=SHADER_RENDER,
        handler=shader_render,
        description=SHADER_RENDER["description"],
        emoji="🌈",
    )
    ctx.register_tool(
        name="shader_library",
        toolset="shaderbruecke",
        schema=SHADER_LIBRARY,
        handler=shader_library,
        description=SHADER_LIBRARY["description"],
        emoji="📚",
    )
    skill = Path(__file__).with_name("skills") / "shader-craft" / "SKILL.md"
    if skill.is_file():
        ctx.register_skill(
            "shader-craft", skill,
            "Use when making, fixing, tuning or exporting a shader with Shaderbrücke: the render-"
            "see-fix loop, sliders, versions and forks, the export proofs, and learning mode.")
    import functools
    config = {
        "allow_project_writes": bool(ctx.get_config("allow_project_writes", False)),
        "credit_line": bool(ctx.get_config("credit_line", True)),
    }
    ctx.register_tool(
        name="shader_export",
        toolset="shaderbruecke",
        schema=SHADER_EXPORT,
        handler=functools.partial(shader_export, config=config),
        description=SHADER_EXPORT["description"],
        emoji="📦",
    )
