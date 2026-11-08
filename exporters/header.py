"""The self-describing header every exported file starts with (one builder, all targets)."""

from __future__ import annotations

import re

CREDIT = "Made with Shaderbrücke — https://github.com/studioluftbruecke/shaderbruecke"
PRIVATE_LICENCE = "unknown (private; the author has not chosen one)"


def pascal(name: str | None) -> str:
    """'golden-hour sky' -> 'GoldenHourSky' (a valid JS/TS identifier)."""
    parts = re.split(r"[^A-Za-z0-9]+", str(name or ""))
    joined = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not joined:
        return "Shader"
    return "Shader" + joined if joined[0].isdigit() else joined


def slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40] or "shader"


def slider_line(record: dict) -> str:
    sliders = record.get("sliders") or []
    if not sliders:
        return "Sliders: none"
    parts = []
    for slider in sliders:
        piece = f"{slider['name']} {slider['min']:g}..{slider['max']:g} (default {slider['default']:g}"
        if slider.get("step"):
            piece += f", step {slider['step']:g}"
        parts.append(piece + ")")
    return "Sliders: " + ", ".join(parts)


def provenance_line(record: dict) -> str:
    provenance = record.get("provenance") or {}
    bits = [f"origin {provenance.get('origin') or 'agent'}"]
    for key in ("author", "url", "parent"):
        if provenance.get(key):
            bits.append(f"{key} {provenance[key]}")
    return "Provenance: " + ", ".join(bits)


def build_header(record: dict, target_label: str, *, credit: bool = True,
                 comment: str = "//") -> str:
    sliders = record.get("sliders") or []
    lines = [
        f"{record.get('name') or 'shader'} — {record.get('id')}@{record.get('version')}",
        f"Target: {target_label}",
        "Uniforms: iResolution (pixels), iTime (seconds), iTimeDelta, iFrame, iMouse, iDate; "
        "fragCoord is in pixels, origin bottom left.",
        slider_line(record),
        provenance_line(record),
        "Licence: " + ((record.get("provenance") or {}).get("licence") or PRIVATE_LICENCE),
    ]
    if sliders:
        lines.append("Set sliders through the exported uniforms; do not rewrite the shader maths.")
    if credit:
        lines.append(CREDIT)
    return "\n".join(f"{comment} {line}" for line in lines) + "\n"
