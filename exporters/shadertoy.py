"""Shadertoy export: one paste-ready Image tab, with sliders as `const float`s."""

from __future__ import annotations

from .header import build_header, effective_sliders, pascal


def glsl_float(value: float) -> str:
    """A GLSL ES float literal. `5` is an int there, and `const float x = 5;` does not compile."""
    text = f"{float(value):g}"
    if "." not in text and "e" not in text:
        text += ".0"
    return text


def source_tab(record: dict, *, credit: bool = True) -> str:
    header = build_header(record, "Shadertoy Image tab", credit=credit)
    sliders = effective_sliders(record)
    parts = [header.rstrip("\n"), ""]
    if sliders:
        parts.append("#define SHADERBRUECKE_SLIDERS__edit_the_constants_below")
        parts += [f"const float {slider['name']} = {glsl_float(slider['default'])};"
                  for slider in sliders]
        parts.append("")
    parts += [(record.get("source") or "").strip("\n"), ""]
    return "\n".join(parts)


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {f"{name}.shadertoy.glsl": source_tab(record, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    return [
        "Open shadertoy.com/new and paste the file into the Image tab.",
        "Shadertoy asks you to be signed in to save; the shader needs no channels.",
        "Edit the `const float` values at the top to tune it; Shadertoy has no sliders for these.",
        "If you publish it, keep the provenance and licence lines in the header.",
    ]
