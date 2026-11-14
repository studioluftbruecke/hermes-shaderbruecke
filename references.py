"""`@shader:<id>[@v<n>]`: one shader version as context for the agent's turn.

Hermes keeps one provider per prefix for the whole process, and that table is shared by every
profile the gateway serves. So the provider (`make_shim`) decides per call: it answers only where
the running profile's config enables Shaderbrücke, and `expand` / `complete` read that profile's
library (plugin storage follows the active profile). Elsewhere it gives `None` / `[]`, as if the
prefix did not exist. The text: bounded source, all sliders and tuned values, one line each for licence,
lineage and the check; `shader_library get` for the full source and the rest.

The provider runs the code of the first Shaderbrücke the process loaded, for every profile.
"""

from __future__ import annotations

import re

PLUGIN = "shaderbruecke"
PREFIX = "shader"
DESCRIPTION = "A Shaderbrücke shader version: source, sliders, tuned values, licence, lineage, check"
MAX_EXPANSION_CHARS = 6000
_TARGET_RE = re.compile(r"^(?P<id>[a-z0-9][a-z0-9-]{0,79})(?:@v(?P<version>\d{1,6}))?$")
_QUOTES = "`\"'"


def parse_target(target: str) -> tuple[str, int | None] | None:
    """`<id>` or `<id>@v<n>` (optionally quoted, trailing punctuation dropped); None otherwise."""
    text = str(target or "").strip().strip(_QUOTES).rstrip(",.;!?)")
    match = _TARGET_RE.fullmatch(text)
    if not match:
        return None
    version = match.group("version")
    return match.group("id"), int(version) if version else None


def _number(value) -> str:
    return f"{float(value):g}"


def slider_lines(doc: dict) -> list[str]:
    from .record import tuned_values

    tuned = tuned_values(doc)
    lines = []
    for slider in doc.get("sliders") or []:
        line = (f"- {slider['name']}: {_number(slider['min'])}..{_number(slider['max'])}, "
                f"default {_number(slider['default'])}")
        if slider.get("step"):
            line += f", step {_number(slider['step'])}"
        if slider["name"] in tuned:
            line += f", tuned {_number(tuned[slider['name']])}"
        if slider.get("label"):
            line += f' ("{slider["label"]}")'
        lines.append(line)
    return lines


def licence_line(doc: dict, meta: dict | None) -> str:
    licence = (doc.get("provenance") or {}).get("licence")
    inherited = (meta or {}).get("licence_inherited")
    text = f"Licence: {licence}" if licence else "Licence: none chosen (private)"
    if inherited:
        text += f"; inherited {inherited} from the imported original, which fixes it"
    return text


def lineage_line(doc: dict) -> str:
    provenance = doc.get("provenance") or {}
    parent = provenance.get("parent")
    if parent:
        version = provenance.get("parent_version")
        return f"Lineage: fork of {parent}" + (f"@v{version}" if version else "")
    if provenance.get("origin") == "import":
        bits = [b for b in (provenance.get("author") and f"by {provenance['author']}",
                            provenance.get("url") and f"from {provenance['url']}") if b]
        return "Lineage: imported" + (" " + " ".join(bits) if bits else "")
    return "Lineage: original"


def check_line(doc: dict) -> str:
    checks = doc.get("checks") or {}
    flash = (checks.get("flash") or {}).get("verdict") or "unknown"
    motion = (checks.get("motion") or {}).get("verdict") or "unknown"
    flat = "flat frames" if checks.get("flat") is True else "not flat"
    return (f"Check: {checks.get('status') or 'unknown'}, flash {flash}, motion {motion}, {flat} "
            f"(rendered {doc.get('created_at') or 'unknown'})")


def expansion(doc: dict, *, latest_version: int, meta: dict | None = None) -> str:
    """The text one `@shader:` reference adds to the turn."""
    name = (meta or {}).get("name") or doc.get("name") or "shader"
    from .exporters.header import code_fence
    from .record import author_kind

    who = author_kind(doc)
    source = str(doc.get("source") or "").rstrip()
    # The source can come from a share file or a paste: a fence longer than any backtick run in
    # it keeps the text inside the block.
    opening, closing = code_fence(source, "glsl")
    lines = [
        f'Shaderbrücke shader "{name}" (id {doc["id"]}, version {doc["version"]} of {latest_version}, '
        f"written by {'the user' if who == 'human' else 'an import' if who == 'import' else 'the agent'}).",
        "Sliders (each one is a `uniform float`; the `// @slider` lines in the source declare them):",
        *(slider_lines(doc) or ["- none"]),
        licence_line(doc, meta),
        lineage_line(doc),
        check_line(doc),
        "Source (Shadertoy-style GLSL ES 3.00 body, `mainImage`):",
        opening,
        source,
        closing,
        f"Play it in the chat: shader_library play with id {doc['id']} and version {doc['version']} "
        "(not a new render).",
        "Frames, exports and other versions: shader_library get with this id. A new version: "
        "shader_render with id " + doc["id"] + ", keeping the tuned values as the `@slider` defaults.",
    ]
    text = "\n".join(lines)
    if len(text) <= MAX_EXPANSION_CHARS:
        return text

    source_at = len(lines) - 4
    head = "\n".join(lines[:source_at])
    notice = (f"Source truncated for context. Full source: shader_library get with id {doc['id']} "
              f"and version {doc['version']}.")
    tail = "\n".join([lines[source_at + 1], notice, *lines[source_at + 2:]])
    available = MAX_EXPANSION_CHARS - len(head) - len(tail) - 2
    if available >= 0:
        return "\n".join((head, source[:available], tail))

    # Oversized metadata must not defeat the source budget. Keep the id, a valid empty fence and the full-source route.
    compact = [f'Shaderbrücke shader id {doc["id"]}, version {doc["version"]} of {latest_version}.',
               "Source omitted because its metadata alone exceeded the context budget.", "```glsl", "```", notice]
    return "\n".join(compact)


def expand_from(conn, target: str) -> str | None:
    from . import record

    parsed = parse_target(target)
    if parsed is None:
        return None
    shader_id, version = parsed
    newest = record.latest(conn, shader_id)
    if newest is None:
        return None
    doc = newest if version is None else record.get_version(conn, shader_id, version)
    if doc is None:
        return None
    meta = record.get_meta(conn, shader_id)
    return expansion(doc, latest_version=int(newest["version"]), meta=meta)


def complete_from(conn, query: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """`(text, display, meta)` per matching shader, newest first: text is `<id>@v<latest>`."""
    from . import record

    needle = str(query or "").strip().lower()
    rows = []
    for doc in record.list_recent(conn, 200):
        meta = record.get_meta(conn, doc["id"])
        name = meta["name"]
        if meta.get("archived_at"):
            continue
        if needle and needle not in name.lower() and needle not in doc["id"]:
            continue
        rows.append((f"{doc['id']}@v{doc['version']}", record.display_name(name),
                     f"v{doc['version']}, {doc.get('created_at') or ''}".strip(", ")))
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def _storage():
    import contextlib

    from plugins.plugin_storage import plugin_data_dir, plugin_db

    from . import record

    conn = plugin_db(PLUGIN)
    record.ensure_schema(conn)
    return contextlib.closing(conn), plugin_data_dir(PLUGIN)


def expand(target: str) -> str | None:
    """Live: the running profile's library (called through the shim)."""
    closing, _base = _storage()
    with closing as conn:
        return expand_from(conn, target)


def complete(query: str, limit: int = 10) -> list[tuple[str, str, str]]:
    closing, _base = _storage()
    with closing as conn:
        return complete_from(conn, query, limit)


# ---------------------------------------------------------------- the provider (registered once)

def profile_enables_plugin() -> bool:
    """Whether the running profile's config.yaml enables Shaderbrücke (Hermes caches the read)."""
    try:
        from hermes_cli.config import load_config

        plugins = load_config().get("plugins")
    except Exception:
        return False
    return isinstance(plugins, dict) and PLUGIN in (plugins.get("enabled") or []) \
        and PLUGIN not in (plugins.get("disabled") or [])


def expand_if_enabled(target: str) -> str | None:
    return expand(target) if profile_enables_plugin() else None


def complete_if_enabled(query: str, limit: int = 10) -> list[tuple[str, str, str]]:
    return complete(query, limit) if profile_enables_plugin() else []


def make_shim():
    """The provider Hermes registers for `@shader:`; None outside Hermes."""
    try:
        from agent.context_references import ContextCompletionItem, ContextReferenceProvider
    except ImportError:
        return None
    import asyncio

    class ShaderReferences(ContextReferenceProvider):
        prefix = PREFIX
        description = DESCRIPTION

        async def expand(self, target: str):
            return await asyncio.to_thread(expand_if_enabled, target)

        async def autocomplete(self, query: str, *, limit: int = 10) -> list:
            rows = await asyncio.to_thread(complete_if_enabled, query, limit)
            return [ContextCompletionItem(text=text, display=display, meta=meta)
                    for text, display, meta in rows]

    return ShaderReferences()
