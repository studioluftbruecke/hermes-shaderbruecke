"""Shadertoy export: one paste-ready Image tab, with sliders as `const float`s."""

from __future__ import annotations

from .header import build_header, pascal


def source_tab(record: dict, *, credit: bool = True) -> str:
    header = build_header(record, "Shadertoy Image tab", credit=credit)
    sliders = record.get("sliders") or []
    constants = "".join(f"const float {slider['name']} = {slider['default']:g};\n" for slider in sliders)
    return "\n".join([
        header.rstrip("\n"),
        "",
        "#define SHADERBRUECKE_SLIDERS__edit_the_constants_below" if sliders else "",
        constants.rstrip("\n"),
        "",
        (record.get("source") or "").strip("\n"),
        "",
    ]).replace("\n\n\n", "\n\n")


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
