"""shaderbruecke: the agent's render-see-fix loop for GLSL shaders."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

from . import checks, exporting, jobs, library, record, references, renderer, thumbs, unattended
from . import settings as settings_mod
from .exporters import registry

PLUGIN = "shaderbruecke"
MAX_SOURCE_BYTES = 64 * 1024
# From the third version of one shader within 15 minutes, the result tells the agent to stop and
# show it, and a fourth is refused unless keep_iterating=true: a sentence alone did not stop a
# weaker model, which rendered 11 versions in one turn.
RENDER_BUDGET = 3
BUDGET_WINDOW_S = 15 * 60

SHADER_RENDER = {
    "name": "shader_render",
    "description": (
        "Compile, render and check a Shadertoy-style GLSL ES 3.00 fragment shader on the Hermes "
        "host. Write only the body: `void mainImage(out vec4 fragColor, in vec2 fragCoord)` plus "
        "helper functions. The uniforms iResolution (vec3), iTime, iTimeDelta, iFrame, iMouse and "
        "iDate are injected; do not declare #version, precision, uniforms or main(). Use constant "
        "loop bounds. Tunable numbers become sliders: a line "
        "`// @slider name 0.0..1.0 = 0.5 step 0.01` declares a uniform and a Desktop slider "
        "(an optional quoted label after it, `\"Sun height\"`, names the slider for people). "
        "Before the browser starts, a lint checks for the mistakes that hang a GPU or fail to "
        "compile (`while (true)`, webgl1 names, redeclared uniforms, a second main()). Then it "
        "renders frames at t = 0, 1 and 2.5 s, builds a contact sheet, measures whether the frames "
        "are flat or black, counts NaN pixels and scores the flash risk. After a successful "
        "render: call vision_analyze on `contact_sheet` and look at the frames before you "
        "describe the result; then put the `media` line on its own line in your reply so Hermes "
        "Desktop plays the shader live. A `MEDIA:` line is only returned for a render that is not "
        "flat and not flash-blocked. Pass `id` to keep a new version of an existing shader and "
        "`parent_id` to fork one; versions are kept and listed by shader_library. The same source as "
        "the latest version of `id` writes no new version: the result is that version with "
        "`unchanged: true`. To show an existing version, call shader_library play instead. Budget: about "
        "three renders per request; a fourth version of one shader within 15 minutes is refused "
        "unless the user asked for more and you pass keep_iterating=true."
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
                "description": ("Id of an existing shader, or the id a failed render returned: this "
                                "render becomes its next version. Omit it for a new shader."),
            },
            "keep_iterating": {
                "type": "boolean",
                "description": ("Only when the user asked for more iterations: allows a fourth or later "
                                "version of the same shader within 15 minutes, which is refused otherwise."),
            },
            "parent_id": {
                "type": "string",
                "description": "Id of a shader to fork: a new id is created with lineage to it.",
            },
        },
        "required": ["source"],
    },
}

LIBRARY_ACTIONS = ("list", "get", "play", "fork", "rename", "tag", "archive", "unarchive", "set_licence",
                   "import", "share")

SHADER_LIBRARY = {
    "name": "shader_library",
    "description": (
        "The shaders this profile has rendered (the Shaderbrücke page in Desktop shows the same library). "
        "`list` returns the newest 20 (id, name, version, states, licence, tags, thumbnail), filtered by "
        "q, state, tag, starred or archived, with a `cursor` for the next page. `get` returns one full "
        "record: source, slider specs, `tuned_values` "
        "(the human's slider settings from Desktop), `effective_values` (what every export uses), "
        "checks and provenance. Read them before an export or a new version. `play` shows one version "
        "(default latest) in the chat as the live card with its sliders, without rendering or writing a "
        "version: put its `media` line on its own line in your reply. A `MEDIA:` line becomes that card "
        "only from a `play`, `import` or shader_render result, so never copy a path from `get`. `fork` copies a "
        "shader (or one `version` of it) into a new id and records the lineage, so you can change "
        "it without losing the original. `rename`, `tag` (the full list) and `archive` / `unarchive` "
        "organise the library on the user's request; there is no delete (the user deletes in "
        "Desktop). `set_licence` sets the licence the user picked: it is "
        "stamped on the latest version and every later one, earlier versions keep theirs, and a "
        "licence inherited from an imported original (NC or SA) cannot be replaced. `import` brings "
        "in a share file (`path`) or a pasted shader (`source` with `url`, `author`, `licence` as "
        "provenance): it is linted and rendered before it lands, nothing is fetched. `share` writes "
        "the shader's share file (`<name>.shaderbruecke.json`) that anyone with Shaderbrücke can import."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(LIBRARY_ACTIONS)},
            "id": {"type": "string", "description": "Shader id for get, play, fork and the organising actions."},
            "version": {"type": "integer", "description": "Version for get, play, fork and share (default: latest)."},
            "name": {"type": "string", "description": "Title for the fork, or the new name for rename."},
            "limit": {"type": "integer", "description": "How many rows `list` returns (max 50)."},
            "q": {"type": "string", "description": "For list: part of a name, id or tag."},
            "state": {"type": "string", "enum": ["ok", "flat", "flash_warn", "flash_block", "no_thumb",
                                                 "frames_pruned", "legacy"],
                      "description": "For list: only shaders in this state."},
            "tag": {"type": "string", "description": "For list: only shaders with this tag."},
            "archived": {"type": "boolean", "description": "For list: list the archive instead."},
            "starred": {"type": "boolean", "description": "For list: only starred shaders."},
            "sort": {"type": "string", "enum": ["updated", "created", "name"],
                     "description": "For list: order (default last edited; starred first)."},
            "cursor": {"type": "string", "description": "For list: the `cursor` of the previous page."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "For tag: the shader's full new tag list (lowercase, max 8, 24 chars each)."},
            "licence": {"type": "string", "description": (
                "For set_licence: an SPDX id (MIT, CC0-1.0, CC-BY-4.0, CC-BY-SA-4.0, CC-BY-NC-4.0, "
                "CC-BY-NC-SA-4.0, CC-BY-NC-SA-3.0) or 'private'. Only on the user's request. For "
                "import: the licence the original names (the import keeps an NC or SA licence).")},
            "source": {"type": "string", "description": (
                "For import: a pasted Shadertoy (mainImage), glslCanvas (u_time, gl_FragColor) or ISF "
                "shader. Single pass, no textures or audio. Nothing is fetched.")},
            "path": {"type": "string", "description": (
                "For import: a .shaderbruecke.json share file inside the plugin's data folder.")},
            "url": {"type": "string", "description": "For import: where the original was published."},
            "author": {"type": "string", "description": "For import: who wrote the original."},
            "dest_dir": {"type": "string", "description": (
                "For share: an existing absolute directory to also write the share file into.")},
            "overwrite": {"type": "boolean", "description": "For share: replace a file in dest_dir."},
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
        "that factory), `shadertoy` (a paste-ready Image tab), `agent` (one Markdown handoff "
        "block for another agent or a teammate), `td` (a TouchDesigner GLSL TOP pixel shader; the "
        "user binds iTime and each slider on the Vectors page, as the steps list) and `isf` (an "
        "ISF 2.0 .fs generator for VDMX or Resolume Wire, sliders as INPUTS, no audio yet). `td` "
        "and `isf` are experimental: proven in a WebGL2 shim of the host's wrapper, not yet in "
        "the host itself. Every "
        "file starts with a self-describing header: name, id@version, target, the uniform contract, "
        "the sliders with their ranges, provenance and licence. The three and r3f exports are "
        "rendered in an offline three r187 harness and compared frame by frame with the source: "
        "the result says `match`, `differs` or `not_proven` and why, never a silent pass. Files "
        "always land in the shader's own exports folder; pass `dest_dir` (an existing absolute "
        "directory) to also copy them into a project. The copy happens after the proof and only "
        "on `match`, unless `accept_unproven=true`; it never overwrites unless `overwrite=true`."
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
            "accept_unproven": {"type": "boolean",
                                "description": ("Copy into dest_dir even when the proof is not "
                                                "`match`. Default false; tell the user if you set it.")},
        },
        "required": ["id", "target"],
    },
}

# Kept for older callers and tests; the implementation lives in exporting.py.
PROOF_MEAN_LIMIT = exporting.PROOF_MEAN_LIMIT
PROOF_P99_LIMIT = exporting.PROOF_P99_LIMIT
dest_decision = exporting.dest_decision


def _numbered(source: str, lines: set[int]) -> str:
    """The source lines named by errors, with one line of context, numbered like the log."""
    src = source.splitlines()
    keep = sorted({n for line in lines for n in (line - 1, line, line + 1) if 1 <= n <= len(src)})
    return "\n".join(f"{'>' if n in lines else ' '}{n:4d} | {src[n - 1]}" for n in keep)


def _storage():
    """The profile's database and data directory. Called inside handlers, never at import."""
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN), _data_dir()


def _data_dir():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN)


def _id_error(field: str, value) -> str:
    return (f"invalid {field} {value!r}: shader ids are lowercase letters, digits and '-' "
            "(take one from shader_library list)")


def _check_report(bundle: renderer.BundleResult, flash: dict, lint: list[dict]) -> dict:
    return {
        "status": bundle.status,
        "diagnostics": [{"severity": e.severity, "where": e.where, "line": e.line,
                         "message": e.message} for e in bundle.errors],
        "lint": lint,
        "frames": bundle.frames,
        "contact_sheet": bundle.contact_sheet,
        "flat": checks.flat(bundle.frames),
        "motion": checks.motion(bundle.frames, bundle.late),
        "flash": flash,
        "wall_ms": bundle.wall_ms,
        "draw_ms": bundle.draw_ms,
        "renderer": bundle.renderer,
    }


def _record_document(shader_id: str, version: int, name: str, source: str, sliders: list[dict],
                     parent_id: str | None, report: dict, frag: Path, bundle: renderer.BundleResult,
                     *, licence: str | None = None, parent_version: int | None = None,
                     author_kind: str = "agent", created_at: str | None = None,
                     provenance: dict | None = None) -> dict:
    """One version. `author_kind` (who wrote the source), `provenance.parent_version` and
    `provenance.licence` (stamped from the shader's meta: the licence picked when the version was
    made) are written once here."""
    return {
        "id": shader_id,
        "version": version,
        "name": name,
        "source": source,
        "sliders": sliders,
        "tuned_values": {},
        "channels": [],
        "provenance": {"origin": "agent", "url": None, "author": None, "licence": licence,
                       "parent": parent_id, "parent_version": parent_version, **(provenance or {})},
        "checks": report,
        "edited_by": "human" if author_kind == "human" else "agent",
        "author_kind": author_kind,
        "created_at": created_at or record.now_iso(),
        "files": {"frag": str(frag), "contact_sheet": bundle.contact_sheet,
                  "snapshot": next((f.get("png_path") for f in bundle.frames), None),
                  "thumb": bundle.thumb},
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
    except OverflowError:
        t = math.inf                         # an integer too large for a float
    except (TypeError, ValueError):
        t = 1.0
    if not math.isfinite(t):
        return json.dumps({"success": False, "error": "time must be a finite number of seconds"})
    requested_id = args.get("id")
    requested_id = str(requested_id).strip() if requested_id not in (None, "") else None
    parent_id = args.get("parent_id")
    parent_id = str(parent_id).strip() if parent_id not in (None, "") else None
    if requested_id is not None and not record.valid_id(requested_id):
        return json.dumps({"success": False, "error": _id_error("id", requested_id)})
    if parent_id is not None and not record.valid_id(parent_id):
        return json.dumps({"success": False, "error": _id_error("parent_id", parent_id)})

    conn, base = _storage()
    with contextlib.closing(conn):
        # Same source as the latest version: that version again, not a duplicate with no tuned values.
        newest = record.latest(conn, requested_id) if requested_id and not parent_id else None
        if newest is not None and newest.get("source") == source:
            return json.dumps(_card_payload(conn, newest, unchanged=True))
        with renderer.chromium_slots(Path(base) / "locks"):
            return _render(conn, base, source=source, name=name, t=t, requested_id=requested_id,
                           parent_id=parent_id, keep=settings_mod.read_settings()["keep_versions"],
                           keep_iterating=args.get("keep_iterating") is True)


def _render(conn, base, *, source: str, name: str, t: float = 1.0, requested_id: str | None = None,
            parent_id: str | None = None, keep: int = 5, keep_iterating: bool = False,
            adopt: dict | None = None, author_kind: str = "agent", provenance: dict | None = None,
            meta_defaults: dict | None = None, expected_base: int | None = None) -> str:
    """One render that becomes a version on success. `adopt` (`{id, created_at}`) re-renders a
    flat folder of the first builds into its v1; `author_kind` and `provenance` let the Lab
    and the importer reuse the same path; `meta_defaults` seeds a new shader's meta (an import's
    inherited licence). `expected_base` (the Lab's save) refuses with `stale_base` when another
    version landed during the render."""
    record.ensure_schema(conn)
    if parent_id and record.latest(conn, parent_id) is None:
        return json.dumps({"success": False, "error": f"no shader with id {parent_id} to fork"})
    if adopt is not None:
        if record.latest(conn, adopt["id"]) is not None:
            return json.dumps({"success": False, "error": f"{adopt['id']} already has versions"})
        requested_id = None
    if requested_id and not parent_id and record.latest(conn, requested_id) is None:
        attempt = record.attempt_meta(base, requested_id)
        if attempt is None:
            return json.dumps({"success": False, "error": (
                f"no shader with id {requested_id}: `id` continues an existing shader. "
                "For a new shader omit id; to fork an existing one pass parent_id.")})
        # The id of a failed first render: continue it as version 1, with that attempt's lineage.
        parent_id = attempt.get("parent_id")
        shader_id = requested_id
    elif adopt is not None:
        shader_id = adopt["id"]
    else:
        shader_id = record.new_id(name) if parent_id or not requested_id else requested_id
    previous = record.latest(conn, shader_id) if requested_id and not parent_id else None
    if previous is not None and not keep_iterating:
        recent = record.recent_versions(conn, shader_id, within_s=BUDGET_WINDOW_S)
        if recent >= RENDER_BUDGET:
            frag_path = (previous.get("files") or {}).get("frag")
            refusal = {"success": False, "id": shader_id, "latest_version": previous.get("version"),
                       "error": (f"render_budget: {recent} versions of this shader in the last 15 minutes; "
                                 "a further one needs keep_iterating=true"),
                       "next": ("Show the latest version now: put the `media` line on its own line in your "
                                "reply and offer further changes in words. Pass keep_iterating=true only "
                                "if the user asked for more iterations.")}
            if frag_path:
                refusal["media"] = f"MEDIA:{frag_path}"
            return json.dumps(refusal)
    version = record.next_version(conn, shader_id)
    # Every attempt renders into a scratch folder; only a successful render becomes a version.
    out_dir = record.attempt_dir(base, shader_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    record.write_attempt_meta(out_dir, parent_id=parent_id)
    frag = out_dir / "shader.frag"
    frag.write_text(source, encoding="utf-8")

    sliders = record.parse_sliders(source)
    findings = checks.lint(source, slider_names=[slider["name"] for slider in sliders])
    lint_payload = [f.as_dict() for f in findings]
    lint_errors = [f for f in findings if f.severity == "error"]
    if lint_errors:
        payload = {
            "success": False,
            "error": f"lint_error: {len(lint_errors)} error(s), {len(findings) - len(lint_errors)} warning(s)",
            "id": shader_id,
            "version": version,
            "lint": lint_payload,
            "next": ("Fix every lint error and call shader_render again with the full corrected source "
                     "and the same id (it becomes version 1; a fork keeps its parent). "
                     + "; ".join(f"line {f.line}: {f.hint}" for f in lint_errors[:4])),
        }
        (out_dir / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        record.prune_attempts(base, shader_id, keep=out_dir)
        return json.dumps(payload)

    times = tuple(sorted({0.0, max(0.0, t), 2.5}))
    bundle = renderer.render_bundle(source, out_dir, times=times, sliders=sliders)
    flash = checks.flash_verdict(bundle.flash.get("lumas", []), bundle.flash.get("reds", []),
                                 fps=bundle.flash.get("fps", 60.0)) if bundle.flash else \
        {"max_general_per_s": 0, "red": False, "verdict": "none", "pairs": 0, "frames": 0}

    if bundle.status != "ok":
        (out_dir / "result.json").write_text(json.dumps(bundle.as_dict(), indent=2), encoding="utf-8")
        record.prune_attempts(base, shader_id, keep=out_dir)

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
            "next": ("Fix these lines and call shader_render again with the full corrected source and "
                     "the same id (a first render that failed becomes version 1; a fork keeps its parent)."),
        })

    if bundle.status != "ok":
        failure = {"success": False, "error": bundle.detail or bundle.status,
                   "status": bundle.status, "id": shader_id, "version": version,
                   "lint": lint_payload}
        if bundle.phase:
            failure["phase"] = bundle.phase
        if bundle.status == "busy":
            failure["next"] = ("Other renders for this profile are still running. Call shader_render "
                               "again in a moment with the same source.")
        return json.dumps(failure)

    # Number, promote and store under the shader's lock: another render of this shader may have
    # landed while this one was in the browser.
    with record.shader_lock(base, shader_id):
        newest = record.latest(conn, shader_id)
        if previous is not None and newest is None:
            # Deleted while this render ran: storing it would bring the shader back as v1.
            gone = {"success": False, "status": "not_found", "id": shader_id,
                    "error": f"shader {shader_id} was deleted while this render ran; nothing was stored"}
            shutil.rmtree(out_dir, ignore_errors=True)
            return json.dumps(gone)
        moved = (adopt is not None and newest is not None) or (
            expected_base is not None and int((newest or {}).get("version") or 0) != expected_base)
        if moved:
            latest_version = int((newest or {}).get("version") or 0)
            stale = {"success": False, "status": "stale_base", "id": shader_id, "latest": latest_version,
                     "error": (f"{adopt['id']} already has versions" if adopt is not None else
                               f"a newer version exists (v{latest_version}); this edit started from "
                               f"v{expected_base}")}
            (out_dir / "result.json").write_text(json.dumps(stale, indent=2), encoding="utf-8")
            record.prune_attempts(base, shader_id, keep=out_dir)
            return json.dumps(stale)
        if requested_id and not parent_id:
            previous = newest
        version = int((newest or {}).get("version") or 0) + 1
        # Promote the attempt to its version folder and point every stored path there.
        final_dir = record.version_dir(base, shader_id, version)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(out_dir, final_dir)
        (final_dir / record.ATTEMPT_META).unlink(missing_ok=True)
        record.prune_attempts(base, shader_id, keep=None)
        bundle.frames = record.rebase(bundle.frames, str(out_dir), str(final_dir))
        bundle.contact_sheet = record.rebase(bundle.contact_sheet, str(out_dir), str(final_dir))
        out_dir = final_dir
        frag = final_dir / "shader.frag"
        (out_dir / "result.json").write_text(json.dumps(bundle.as_dict(), indent=2), encoding="utf-8")

        is_flat = checks.flat(bundle.frames)
        blocked = flash.get("verdict") == "block"
        report = _check_report(bundle, flash, lint_payload)
        if version == 1:
            meta = record.new_meta(shader_id, name)
            if parent_id:
                parent_meta = record.get_meta(conn, parent_id)
                meta.update(licence=parent_meta["licence"], licence_inherited=parent_meta["licence_inherited"])
            meta.update(meta_defaults or {})
            record.save_meta(conn, meta)
        else:
            meta = record.update_meta(conn, shader_id)
        # A later version of a fork keeps the fork's lineage.
        lineage, parent_version = parent_id, None
        if parent_id:
            parent_version = int((record.latest(conn, parent_id) or {}).get("version") or 0) or None
        elif previous is not None:
            lineage = (previous.get("provenance") or {}).get("parent")
            parent_version = (previous.get("provenance") or {}).get("parent_version")
        record.insert(conn, _record_document(
            shader_id, version, name, source, sliders, lineage, report, frag, bundle,
            licence=meta["licence"], parent_version=parent_version, author_kind=author_kind,
            created_at=adopt["created_at"] if adopt else None, provenance=provenance))
        record.prune_versions(base, shader_id, keep=keep)
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
        "browser_sandbox": renderer.sandbox_state(),
        "sliders": sliders,
        "tuned_values": {},
        "checks": report,
        "contact_sheet": bundle.contact_sheet,
    }
    if parent_id:
        payload["parent_id"] = parent_id
    carried = _unadopted_tuning(previous, sliders)
    if carried:
        payload["previous_tuned_values"] = carried
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
    times_text = ", ".join(f"{frame['t']:g}" for frame in bundle.frames)
    recent = record.recent_versions(conn, shader_id, within_s=BUDGET_WINDOW_S)
    payload["versions_last_15_min"] = recent
    if recent >= RENDER_BUDGET and "media" in payload:
        payload["next"] += (f" Stop here unless the user asked for more iterations: this is version {recent} "
                            "of this shader in 15 minutes, and a further one is refused without "
                            "keep_iterating=true. Show it now with the media line and offer further "
                            "changes in words.")
    late = report["motion"]["late"]
    late_text = ", ".join(f"{frame['t']:g}" for frame in late)
    if report["motion"]["verdict"] == "still" and not is_flat:
        seen = f"{times_text}, {late_text}" if late_text else times_text
        payload["next"] += (f" The frames at t = {seen} s are the same picture: the shader does not "
                            "move. If it should animate, check that iTime reaches the visible part.")
    elif report["motion"]["slow"]:
        first = next(frame for frame in late if frame["moved"] >= checks.STILL_SHARE)
        payload["next"] += (f" The shader moves slowly: the frames at t = {times_text} s look alike, but "
                            f"{first['moved']:.0%} of pixels differ by t = {first['t']:g} s. That is motion, "
                            "not a bug; speed it up only if the user wants visible movement within seconds.")
    elif report["motion"]["verdict"] == "moving":
        share = report["motion"]["max"]
        payload["next"] += (f" checks.motion is `moving` (up to {share:.0%} of pixels change between frames), "
                            "so time advances even when luma_mean stays the same.")
    if carried:
        tuned_text = ", ".join(f"{key} = {value:g}" for key, value in carried.items())
        payload["next"] += (f" The human tuned v{(previous or {}).get('version')} to {tuned_text}, and this "
                            "version does not use those numbers as defaults. Unless the user asked "
                            "for a new look, write them as the defaults in the `@slider` lines and "
                            "render again.")
    if bundle.slot_wait_ms >= 1000:
        payload["next"] = (f"This render waited {bundle.slot_wait_ms / 1000:.0f} s for a free renderer "
                           f"(at most {renderer.MAX_CHILDREN} run at once per profile). "
                           + payload["next"])
    return json.dumps(payload)


def _unadopted_tuning(previous: dict | None, sliders: list[dict]) -> dict[str, float]:
    """The human's values on the previous version that the new source does not carry as defaults."""
    if not previous:
        return {}
    authored = {s["name"]: float(s["default"]) for s in previous.get("sliders") or []}
    new_defaults = {s["name"]: float(s["default"]) for s in sliders}
    return {name: value for name, value in record.tuned_values(previous).items()
            if value != authored.get(name) and new_defaults.get(name) != value}


def _whole(value) -> int | None:
    """A version or a limit as an int: a whole number, a finite float or digits as text; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value):
        value = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and -2 ** 63 <= value < 2 ** 63 else None


def _bad_numbers(args: dict, *keys: str) -> str | None:
    """The refusal for the first of `keys` that is given but is not a whole number."""
    for key in keys:
        if args.get(key) is not None and _whole(args[key]) is None:
            return f"{key} must be a whole number, got {str(args[key])[:40]!r}"
    return None


def shader_library(args: dict, **kwargs) -> str:
    action = str(args.get("action") or "list").lower()
    if action not in LIBRARY_ACTIONS:
        return json.dumps({"success": False, "error": f"unknown action {action}"})
    shader_id = str(args.get("id") or "").strip()
    if action not in ("list", "import"):
        if not shader_id:
            return json.dumps({"success": False, "error": f"id is required for {action}"})
        if not record.valid_id(shader_id):
            return json.dumps({"success": False, "error": _id_error("id", shader_id)})
    bad = _bad_numbers(args, "version", "limit")
    if bad:
        return json.dumps({"success": False, "error": bad})
    if action == "fork" and args.get("name") is not None and not isinstance(args["name"], str):
        return json.dumps({"success": False, "error": "name must be text"})
    conn, base = _storage()
    with contextlib.closing(conn):
        return _library(conn, base, action, shader_id, args)


def _library(conn, base, action: str, shader_id: str, args: dict) -> str:
    record.ensure_schema(conn)
    if action == "list":
        return json.dumps(_list(conn, base, args))

    if action == "get":
        version = _whole(args.get("version"))
        found = record.latest(conn, shader_id) if version is None else record.get_version(conn, shader_id, version)
        if found is None:
            return json.dumps({"success": False, "error": f"no shader {shader_id}"
                                                          + (f" version {version}" if version else "")})
        meta = record.get_meta(conn, shader_id)
        if meta and meta.get("name"):
            found["record_name"], found["name"] = found.get("name"), meta["name"]
        found["tuned_values"] = record.tuned_values(found)
        found["effective_values"] = record.effective_values(found)
        return json.dumps({"success": True, "shader": _without_frag(found),
                           "next": ("`tuned_values` are the human's slider settings and "
                                    "`effective_values` what every export uses. Keep them as the "
                                    "`@slider` defaults when you write the next version.")})

    if action == "play":
        version = _whole(args.get("version"))
        found = record.latest(conn, shader_id) if version is None else record.get_version(conn, shader_id, version)
        if found is None:
            return json.dumps({"success": False, "error": f"no shader {shader_id}"
                                                          + (f" version {version}" if version else "")})
        return json.dumps(_card_payload(conn, found, unchanged=False))

    if action in ("rename", "tag", "archive", "unarchive"):
        return json.dumps(_organise(conn, shader_id, action, args))
    if action == "set_licence":
        return json.dumps(_set_licence(conn, base, shader_id, args.get("licence")))
    if action == "import":
        return json.dumps(_tool_import(conn, base, args))
    if action == "share":
        return json.dumps(_tool_share(conn, base, shader_id, args))

    forked = record.fork(conn, base, shader_id, name=args.get("name"), version=_whole(args.get("version")))
    if forked is None:
        return json.dumps({"success": False, "error": f"no shader {shader_id} to fork"})
    return json.dumps({"success": True, "shader": _without_frag(forked), "parent_id": shader_id})


def _without_frag(doc: dict) -> dict:
    """The record without its `.frag` path. Desktop draws a `MEDIA:` line with the card of the FIRST
    tool result that names the path, so a `get` or `fork` naming it would take over the line of a
    later `play` in the same turn and show a plain file instead of the card."""
    files = {key: value for key, value in (doc.get("files") or {}).items() if key != "frag"}
    return {**doc, "files": files}


def _no_autoplay_reason(doc: dict) -> str | None:
    """Why a stored version gets no `MEDIA:` line, or None when it may play by itself: the same
    rules as an import (`library.autoplay`, not flat) plus a frag that is still on disk."""
    checks_ = doc.get("checks") or {}
    frag = (doc.get("files") or {}).get("frag")
    if checks_.get("flat") is True:
        return "its frames are flat (one colour or black), so it does not play by itself"
    verdict = library.flash_verdict(doc)
    if checks_.get("status") != "ok":
        return (f"its server check is {checks_.get('status') or 'missing'}, and only a version that "
                "passed the check plays by itself")
    if verdict != "none":
        return (f"its flash check is {verdict or 'missing'}, and only a version with flash verdict "
                "none plays by itself")
    if not frag or not Path(frag).is_file():
        return "its shader.frag is no longer on disk; render the source again to play it"
    return None


def _card_payload(conn, doc: dict, *, unchanged: bool) -> dict:
    """What the Desktop card reads from a render result, for a version that already exists: `play`
    and a render whose source equals the latest version's. Writes nothing."""
    meta = record.get_meta(conn, doc["id"])
    frag = (doc.get("files") or {}).get("frag")
    payload = {"success": True, "id": doc["id"], "version": doc["version"],
               "name": (meta or {}).get("name") or doc.get("name"), "frag_path": frag,
               "sliders": doc.get("sliders") or [], "tuned_values": record.tuned_values(doc),
               "checks": doc.get("checks") or {},
               "contact_sheet": (doc.get("files") or {}).get("contact_sheet")}
    if unchanged:
        payload["unchanged"] = True
    head = (f"Nothing changed: this source is v{doc['version']}, so no new version was written. "
            if unchanged else "Nothing was rendered and no version was written. ")
    reason = _no_autoplay_reason(doc)
    if reason is None:
        payload["media"] = f"MEDIA:{frag}"
        payload["next"] = (head + "Put the `media` line on its own line in your reply: Desktop plays "
                           f"v{doc['version']} live with its sliders, and slider moves save on v{doc['version']}.")
    else:
        payload["warning"] = f"no media line: {reason}"
        payload["next"] = head + f"Tell the user why it does not play here: {reason}."
    return payload


def _list(conn, base, args: dict) -> dict:
    """`list` for the agent: the page's filters and cursor, snake_case fields, the contact sheet as
    the thumbnail (the agent reads it with vision_analyze)."""
    from .dashboard import library_api

    params = {key: args.get(key) for key in ("q", "state", "tag", "sort", "cursor", "licence")
              if args.get(key) is not None}
    params["archived"] = "1" if args.get("archived") is True else ""
    params["starred"] = "1" if args.get("starred") is True else ""
    limit = args.get("limit")
    params["limit"] = max(1, min(50, _whole(limit))) if _whole(limit) is not None else 20
    page = library_api.library_page(conn, base, params)
    shaders = []
    for row in page["rows"]:
        doc = record.latest(conn, row["id"]) or {}
        sheet = (doc.get("files") or {}).get("contact_sheet")
        shaders.append({"id": row["id"], "name": row["name"], "version": row["version"],
                        "versions": row["versions"], "status": (doc.get("checks") or {}).get("status"),
                        "states": row["states"], "licence": row["licence"], "tags": row["tags"],
                        "starred": row["starred"], "archived": row["archived"],
                        "parent": (row["parent"] or {}).get("id"), "author_kind": row["authorKind"],
                        "thumbnail": sheet if sheet and Path(sheet).is_file() else None,
                        "tuned": bool(record.tuned_values(doc)), "created_at": doc.get("created_at"),
                        "updated_at": row["updatedAt"]})
    answer = {"success": True, "count": len(shaders), "total": page["total"], "cursor": page["cursor"],
              "shaders": shaders}
    if page["adoptable"]:
        answer["adoptable"] = page["adoptable"]
    return answer


def _organise(conn, shader_id: str, action: str, args: dict) -> dict:
    if record.latest(conn, shader_id) is None:
        return {"success": False, "error": f"no shader {shader_id}"}
    if action == "rename":
        name = library.clean_name(args.get("name"))
        if name is None:
            return {"success": False, "error": "rename needs a non-empty name"}
        meta = record.update_meta(conn, shader_id, name=name)
        return {"success": True, "id": shader_id, "name": meta["name"],
                "next": "Exports and share files use the new name from now on; earlier exports keep theirs."}
    if action == "tag":
        tags = library.clean_tags(args.get("tags"))
        if isinstance(tags, str):
            return {"success": False, "error": tags}
        meta = record.update_meta(conn, shader_id, tags=tags)
        return {"success": True, "id": shader_id, "tags": meta["tags"]}
    archived = action == "archive"
    record.update_meta(conn, shader_id, archived_at=record.now_iso() if archived else None)
    return {"success": True, "id": shader_id, "archived": archived,
            "next": ("Archived shaders leave the Shaderbrücke page and `list`; `list` with archived=true shows "
                     "them, and only the user can delete a shader, in Desktop.") if archived else
                    "The shader is back on the Shaderbrücke page."}


def _tool_import(conn, base, args: dict) -> dict:
    if args.get("path"):
        root = Path(base).resolve()
        path = Path(str(args["path"])).expanduser().resolve()
        if root not in path.parents or not path.is_file():
            return {"success": False, "error": (f"path must be an existing share file inside {root}; "
                                                "ask the user to put it there or paste its source")}
        if path.stat().st_size > library.SHARE_MAX_BYTES:
            return {"success": False, "error": "the share file is larger than 1 MB"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"success": False, "error": f"not a share file: {exc}"}
        answer = import_shader(conn, base, {"share": data})
    else:
        answer = import_shader(conn, base, {key: args.get(key) for key in ("source", "name", "url", "author", "licence")})
    if not answer.get("ok"):
        return {"success": False, **{k: v for k, v in answer.items() if k != "ok"},
                "next": "Tell the user why it did not land; fix only what the diagnostics name if they asked you to."}
    newest = record.latest(conn, answer["id"]) or {}
    meta = record.get_meta(conn, answer["id"])
    # The card reads name, sliders and tuned values from the result: the import's own arguments are
    # the pasted original, not the converted source the frag holds.
    result = {"success": True, **{k: v for k, v in answer.items() if k != "ok"},
              "name": (meta or {}).get("name") or newest.get("name"), "sliders": newest.get("sliders") or [],
              "tuned_values": record.tuned_values(newest), "checks": newest.get("checks") or {},
              "next": "Tell the user every warning (licence first). Exports and forks of this shader keep its licence."}
    if library.autoplay(newest) and (newest.get("checks") or {}).get("flat") is not True:
        result["media"] = f"MEDIA:{(newest.get('files') or {}).get('frag')}"
        result["next"] += " Put the media line on its own line so Desktop plays it."
    return result


def _tool_share(conn, base, shader_id: str, args: dict) -> dict:
    answer = share_file(conn, base, shader_id, _whole(args.get("version")))
    if not answer:
        return {"success": False, "error": f"no shader {shader_id}"}
    text = json.dumps(answer["share"], indent=1, ensure_ascii=False) + "\n"
    folder = Path(base) / "shares"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / answer["filename"]
    path.write_text(text, encoding="utf-8")
    payload = {"success": True, "id": shader_id, "file": str(path), "bytes": len(text.encode("utf-8")),
               "licence": answer["share"]["licence"], "copied_to": None}
    if args.get("dest_dir"):
        decision = exporting.dest_decision(args["dest_dir"], [answer["filename"]], overwrite=bool(args.get("overwrite")),
                                           allow_writes=bool(settings_mod.read_settings()["allow_project_writes"]),
                                           reason=unattended.unattended_reason(_CTX))
        if not decision["ok"]:
            return {**payload, "success": False, "error": decision["error"]}
        try:
            payload["copied_to"] = exporting.write_into(args["dest_dir"], answer["filename"], text.encode("utf-8"),
                                                        overwrite=bool(args.get("overwrite")))
        except ValueError as exc:
            return {**payload, "success": False, "error": str(exc)}
    payload["next"] = ("Anyone with Shaderbrücke imports this file (the Shaderbrücke page, Import, or shader_library import). "
                       "It carries the licence, author and lineage as claims.")
    return payload


def _set_licence(conn, base, shader_id: str, raw) -> dict:
    newest = record.latest(conn, shader_id)
    if newest is None:
        return {"success": False, "error": f"no shader {shader_id}"}
    licence = None if raw in (None, "", "private", "Private", "none") else str(raw).strip()
    meta = record.get_meta(conn, shader_id)
    refusal = library.licence_change_refusal(meta, licence)
    if refusal:
        allowed = [option["id"] or "private" for option in library.licence_options(meta) if option["allowed"]]
        return {"success": False, "error": "licence_refused", "message": refusal, "allowed": allowed,
                "next": "Tell the user why; do not try another licence on your own."}
    if record.set_licence(conn, base, shader_id, licence) is None:
        return {"success": False, "error": f"no shader {shader_id}"}
    return {"success": True, "id": shader_id, "licence": licence, "stamped_version": newest["version"],
            "next": (f"v{newest['version']} and every later version carry {licence or 'no licence (private)'}; "
                     "earlier versions keep the licence they were made with, and so do their exports.")}


def shader_export(args: dict, **kwargs) -> str:
    shader_id = str(args.get("id") or "").strip()
    if shader_id and not record.valid_id(shader_id):
        return json.dumps({"success": False, "error": _id_error("id", shader_id)})
    bad = _bad_numbers(args, "version")
    if bad:
        return json.dumps({"success": False, "error": bad})
    conn, base = _storage()
    with contextlib.closing(conn):
        record.ensure_schema(conn)
        payload = exporting.export_shader(
            conn, base, shader_id, str(args.get("target") or "").strip(),
            version=_whole(args.get("version")),
            dest_dir=args.get("dest_dir") or None, overwrite=bool(args.get("overwrite")),
            accept_unproven=bool(args.get("accept_unproven")),
            settings=settings_mod.read_settings(),
            unattended_reason=unattended.unattended_reason(_CTX))
    return json.dumps(payload)


def _plugin_version() -> str:
    match = re.search(r'^version:\s*"?([^"\n]+)"?', (Path(__file__).with_name("plugin.yaml")).read_text(
        encoding="utf-8"), re.M)
    return match.group(1) if match else "unknown"


PLUGIN_VERSION = _plugin_version()


def _render_failure(payload: dict) -> dict:
    """A failed render as the contract's inline failure (no version was made)."""
    error = str(payload.get("error") or "render failed")
    code = ("lint_error" if error.startswith("lint_error") else
            "compile_error" if error.startswith(("compile error", "link error")) else
            payload.get("status") or "browser_error")
    diagnostics = [{"line": item.get("line"), "severity": item.get("severity", "error"),
                    "message": item.get("message") or item.get("hint") or ""}
                   for item in (payload.get("lint") or []) if item.get("severity") == "error"]
    diagnostics += [{"line": item.get("line"), "severity": "error", "message": item.get("message") or ""}
                    for item in payload.get("errors") or [] if item.get("where", "shader") == "shader"]
    return {"ok": False, "error": code, "message": error, "diagnostics": diagnostics}


def import_shader(conn, base, payload: dict) -> dict:
    """Import a share file or a pasted Shadertoy, glslCanvas or ISF shader. Nothing is fetched;
    the source is linted and rendered like the agent's before it lands, the claims (author, url,
    licence, lineage, third party) become provenance, and an NC or SA licence becomes inherited."""
    warnings: list[str] = []
    share = payload.get("share")
    if share is not None:
        parsed, why = library.parse_share(share)
        if why:
            return {"ok": False, "error": "bad_share", "message": why, "diagnostics": []}
        dialect, source, name = "shaderbruecke", parsed["source"], parsed["name"]
        licence = parsed["licence"]
        inherited = parsed["licence_inherited"] or (licence if library.inherits(licence) else None)
        author, url = parsed["author"], None
        claims = {"author_url": parsed["author_url"], "lineage": parsed["lineage"],
                  "third_party": parsed["third_party"], "ai_involvement": parsed["ai_involvement"]}
        tags, description, tuned = parsed["tags"], parsed["description"], parsed["tuned_values"]
    else:
        raw = payload.get("source")
        if not isinstance(raw, str) or not raw.strip():
            return {"ok": False, "error": "bad_request", "message": "give a share file or a source", "diagnostics": []}
        if len(raw.encode("utf-8")) > MAX_SOURCE_BYTES:
            return {"ok": False, "error": "bad_request", "message": f"source exceeds {MAX_SOURCE_BYTES} bytes",
                    "diagnostics": []}
        normal = library.normalise_pasted(raw)
        dialect = normal["dialect"]
        if normal["refused"]:
            return {"ok": False, "error": "not_supported", "message": normal["refused"], "dialect": dialect,
                    "diagnostics": []}
        source = normal["source"]
        warnings += normal["warnings"]
        name = library.clean_name(payload.get("name")) or library.clean_name(normal.get("name")) or "imported shader"
        licence = payload.get("licence") or None
        if licence is not None and (not isinstance(licence, str) or not library.known(licence)):
            return {"ok": False, "error": "bad_request", "message": f"unknown licence {licence!r}", "diagnostics": []}
        if library.no_derivatives(licence):
            return {"ok": False, "error": "licence_refused", "message": library.ND_REFUSAL.format(licence=licence),
                    "diagnostics": []}
        if licence is None and dialect == "shadertoy":
            licence = library.SHADERTOY_DEFAULT_LICENCE
            warnings.append("No licence was given, so Shadertoy's default CC BY-NC-SA 3.0 is assumed; "
                            "check the original page and change it only if the author chose another.")
        inherited = licence if library.inherits(licence) else None
        author = library._claim_text(payload.get("author") or normal.get("author"))
        url = library._claim_url(payload.get("url"))
        claims, tags, description, tuned = {}, [], "", {}
    if inherited:
        warnings.append(f"The original is licensed {inherited}: this shader and every fork and export keep that "
                        "licence" + (" and may not be used commercially" if "NC" in inherited else "") + ".")
    provenance = {"origin": "import", "url": url, "author": author, "licence": licence,
                  "imported_name": name, "dialect": dialect, **claims}
    rendered = render_version(conn, base, source=source, name=name, author_kind="import", provenance=provenance,
                              meta_defaults={"licence": licence, "licence_inherited": inherited,
                                             "tags": tags, "description": description})
    if not rendered.get("success"):
        failure = _render_failure(rendered)
        failure["dialect"] = dialect
        return failure
    if tuned:
        record.rewrite(conn, base, rendered["id"], rendered["version"],
                       lambda doc: doc.update(tuned_values=record.tuned_values({**doc, "tuned_values": tuned})))
    if rendered.get("warning"):
        warnings.append(rendered["warning"])
    return {"ok": True, "id": rendered["id"], "version": rendered["version"], "dialect": dialect,
            "licence": licence, "warnings": warnings}


def share_file(conn, base, shader_id: str, version: int | None = None, settings: dict | None = None) -> dict:
    """The share file of one version (`<slug>.shaderbruecke.json`, format shaderbruecke/1)."""
    doc = record.get_version(conn, shader_id, int(version)) if version else record.latest(conn, shader_id)
    if doc is None:
        return {}
    meta = record.get_meta(conn, shader_id)
    kinds = [record.author_kind(item) for item in record.history(conn, shader_id)
             if int(item["version"]) <= int(doc["version"])]
    provenance = doc.get("provenance") or {}
    parent = None
    if provenance.get("parent"):
        parent_doc = (record.get_version(conn, provenance["parent"], int(provenance["parent_version"]))
                      if provenance.get("parent_version") else record.latest(conn, provenance["parent"]))
        parent = {"name": record.display_name(record.get_meta(conn, provenance["parent"])["name"]),
                  "licence": ((parent_doc or {}).get("provenance") or {}).get("licence")}
    share = library.build_share(doc, meta, kinds=kinds, settings=settings or settings_mod.read_settings(),
                                thumb=thumbs.data_url(record.thumb_path(doc)), parent=parent,
                                made_with=f"shaderbruecke {PLUGIN_VERSION}")
    return {"filename": library.slug(share["name"]) + library.SHARE_SUFFIX, "share": share}


SHADER_DELETED = "This shader was deleted."


def save_human_version(conn, base, shader_id: str, source, base_version) -> dict:
    """The Lab's Save. The user's source is linted and rendered like the agent's; on success it
    becomes the next version with `author_kind: human`, and the agent's next turn is told
    (`pre_llm_call`). A save on an older base answers `stale_base`; with both renderers busy it
    answers `busy` at once instead of waiting inside the request."""
    if not isinstance(source, str) or not source.strip():
        return {"refused": 400, "error": "bad_request", "message": "source is required"}
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"refused": 400, "error": "bad_request", "message": f"source exceeds {MAX_SOURCE_BYTES} bytes"}
    if isinstance(base_version, bool) or not isinstance(base_version, int):
        return {"refused": 400, "error": "bad_request", "message": "base_version (the version you edited) is required"}
    newest = record.latest(conn, shader_id)
    if newest is None:
        return {"refused": 404, "error": "not_found", "message": SHADER_DELETED}
    if int(newest["version"]) != base_version:
        return {"refused": 409, "error": "stale_base", "latest": int(newest["version"]),
                "message": f"a newer version exists (v{newest['version']}); you edited v{base_version}"}
    if renderer.free_slots(Path(base) / "locks") == 0:
        return {"refused": 409, "error": "busy",
                "message": f"both renderers are busy ({renderer.MAX_CHILDREN} renders run at once); save again in a moment"}
    rendered = render_version(conn, base, source=source, name=newest.get("name") or shader_id,
                              requested_id=shader_id, author_kind="human", keep_iterating=True,
                              expected_base=base_version)
    if not rendered.get("success"):
        if rendered.get("status") == "busy":
            return {"refused": 409, "error": "busy", "message": str(rendered.get("error"))}
        if rendered.get("status") == "not_found":
            return {"refused": 404, "error": "not_found", "message": SHADER_DELETED}
        if rendered.get("status") == "stale_base":
            return {"refused": 409, "error": "stale_base", "latest": rendered["latest"],
                    "message": str(rendered.get("error"))}
        return _render_failure(rendered)
    record.add_human_notice(conn, base, shader_id, rendered["version"])
    return {"ok": True, "id": shader_id, "version": rendered["version"], "warning": rendered.get("warning")}


_NOTICES_READ: set[str] = set()


def on_pre_llm_call(**_kwargs) -> dict | None:
    """One line for the agent when the user saved a version in the Lab since its last turn. Hooks
    are per profile (each profile's plugin manager holds its own), so the line comes from the
    running profile's library only."""
    try:
        base_dir = _data_dir()
        # Versions before the marker file stored a Lab save as a database row only, so each data
        # folder's database is read once per process; after that the marker alone decides.
        first_read = str(base_dir) not in _NOTICES_READ and (Path(base_dir) / "data.db").exists()
        _NOTICES_READ.add(str(base_dir))
        if not first_read and not record.notices_pending(base_dir):
            return None
        conn, base = _storage()
    except Exception:
        return None
    with contextlib.closing(conn):
        notices = record.take_human_notices(conn, base)
        if not notices:
            return None
        parts = [f'"{record.display_name(record.get_meta(conn, n["id"])["name"])}" (now v{n["version"]}, id {n["id"]})'
                 for n in notices]
    return {"context": ("Shaderbrücke: the user edited " + ", ".join(parts) + " in the Lab since your last turn; "
                        "read it with shader_library get before you change it, and build on their edit.")}


def render_version(conn, base, **kwargs) -> dict:
    """`_render` as a dict, inside the profile's Chromium slots: the adopt job, the Lab's save and
    the importer make versions through the same path as the agent."""
    keep = settings_mod.read_settings()["keep_versions"]
    with renderer.chromium_slots(Path(base) / "locks"):
        return json.loads(_render(conn, base, keep=keep, **kwargs))


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="shaderbruecke_command")
    sub.add_parser("thumbs", help="Render the missing library thumbnails (resumes an interrupted run)")
    sub.add_parser("adopt", help=("Adopt the flat shader folders of the first builds whose render was "
                                  "ok; the others are listed, never changed"))


def _cli(args) -> int:
    command = getattr(args, "shaderbruecke_command", None)
    if command not in {"thumbs", "adopt"}:
        print("usage: hermes shaderbruecke thumbs|adopt")
        return 2
    conn, base = _storage()
    with contextlib.closing(conn):
        record.ensure_schema(conn)
        factory = lambda: _storage()[0]  # noqa: E731
        if command == "thumbs":
            print(f"{len(thumbs.missing(conn))} versions without a thumb in {base}")
            job = thumbs.start_backfill(conn, base, factory, background=False)
        else:
            print(f"{len(thumbs.adoptable(conn, base))} folders to adopt in {base}")
            job = thumbs.start_adopt(conn, base, factory, render_version, background=False)
    print(json.dumps(job, indent=1))
    return 0 if job.get("state") == "done" else 1


# The registering ctx: under `plugins.isolation: host` it is the only reliable source of the
# calling turn's cron and surface signals (unattended.py).
_CTX = None


def register(ctx) -> None:
    """Tools and the skill. Settings are read on every call (settings.py), not here, so a change
    in the settings form applies without a reload."""
    global _CTX
    _CTX = ctx
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
    ctx.register_tool(
        name="shader_export",
        toolset="shaderbruecke",
        schema=SHADER_EXPORT,
        handler=shader_export,
        description=SHADER_EXPORT["description"],
        emoji="📦",
    )
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(name="shaderbruecke", help="Shaderbrücke library maintenance",
                                 setup_fn=_cli_setup, handler_fn=_cli,
                                 description="thumbs: backfill thumbnails; adopt: adopt old flat folders")
    # `@shader:` references. The provider checks the running profile on every call, so it is registered
    # once per process and later loads (other profiles, reloads) are refused harmlessly.
    shim = references.make_shim()
    if shim is not None and hasattr(ctx, "register_context_reference"):
        ctx.register_context_reference(shim)
