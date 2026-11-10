"""The self-describing header every exported file starts with (one builder, all targets).

Every string that reaches a generated file from a record (name, id, author, url, licence) passes
through `header_text` first. Generated files are code, so a newline in a name would otherwise put
text at module level.
"""

from __future__ import annotations

import re

CREDIT = "Made with Shaderbrücke — https://github.com/studioluftbruecke/shaderbruecke"
PRIVATE_LICENCE = "unknown (private; the author has not chosen one)"
HEADER_TEXT_LIMIT = 200

# C0 and C1 controls, DEL, and the Unicode line and paragraph separators (JS line terminators).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def header_text(value, limit: int = HEADER_TEXT_LIMIT) -> str:
    """One safe line of text for a comment, a doc block or a Markdown heading.

    Control characters and line breaks become one space, `*/` becomes `* /` so no block comment
    closes early, and the result is capped at `limit` characters.
    """
    text = _CONTROL_RE.sub(" ", str(value if value is not None else ""))
    text = re.sub(r" {2,}", " ", text).strip()
    while "*/" in text:
        text = text.replace("*/", "* /")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def js_template_literal(text: str) -> str:
    """`text` as the body of a JS template literal whose value is `text` byte for byte.

    Backslash goes first, so the escapes added after it are not doubled. A carriage return is
    written as an escape, because a template literal turns a raw CR or CRLF into LF.
    """
    return (text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            .replace("\r", "\\r"))


def code_fence(text: str, language: str = "") -> tuple[str, str]:
    """Opening and closing Markdown fence lines that no backtick run inside `text` can close."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return ticks + language, ticks


def pascal(name: str | None) -> str:
    """'golden-hour sky' -> 'GoldenHourSky' (a valid JS/TS identifier)."""
    parts = re.split(r"[^A-Za-z0-9]+", str(name or ""))
    joined = "".join(part[:1].upper() + part[1:] for part in parts if part)[:64]
    if not joined:
        return "Shader"
    return "Shader" + joined if joined[0].isdigit() else joined


def slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40] or "shader"


def display_name(record: dict) -> str:
    """The record's name, safe for any generated file."""
    return header_text(record.get("name") or "shader", 80) or "shader"


def slider_line(record: dict) -> str:
    """`name min..max (default d[, tuned t][, step s])` per slider; `tuned` only when the human's
    value differs from the authored default."""
    from ..record import tuned_values

    sliders = record.get("sliders") or []
    if not sliders:
        return "Sliders: none"
    tuned = tuned_values(record)
    parts = []
    for slider in sliders:
        piece = f"{slider['name']} {slider['min']:g}..{slider['max']:g} (default {slider['default']:g}"
        value = tuned.get(slider["name"])
        if value is not None and value != float(slider["default"]):
            piece += f", tuned {value:g}"
        if slider.get("step"):
            piece += f", step {slider['step']:g}"
        parts.append(piece + ")")
    return "Sliders: " + ", ".join(parts)


def effective_sliders(record: dict) -> list[dict]:
    """The sliders as an export writes them: `default` is the effective value (tuned, else the
    authored default) and `authored` keeps the author's number."""
    from ..record import with_effective_defaults

    return with_effective_defaults(record)


def provenance_line(record: dict) -> str:
    provenance = record.get("provenance") or {}
    bits = [f"origin {header_text(provenance.get('origin') or 'agent', 40)}"]
    for key in ("author", "url", "parent"):
        if provenance.get(key):
            bits.append(f"{key} {header_text(provenance[key])}")
    return "Provenance: " + ", ".join(bits)


def licence_text(record: dict) -> str:
    return header_text((record.get("provenance") or {}).get("licence") or "") or PRIVATE_LICENCE


def build_header(record: dict, target_label: str, *, credit: bool = True,
                 comment: str = "//") -> str:
    sliders = record.get("sliders") or []
    lines = [
        f"{display_name(record)} — {header_text(record.get('id'), 80)}"
        f"@{header_text(record.get('version'), 12)}",
        f"Target: {target_label}",
        "Uniforms: iResolution (pixels), iTime (seconds), iTimeDelta, iFrame, iMouse, iDate; "
        "fragCoord is in pixels, origin bottom left.",
        slider_line(record),
        provenance_line(record),
        "Licence: " + licence_text(record),
    ]
    if sliders:
        lines.append("Set sliders through the exported uniforms; do not rewrite the shader maths.")
    if credit:
        lines.append(CREDIT)
    return "\n".join(f"{comment} {line}" for line in lines) + "\n"
