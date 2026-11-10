"""ISF 2.0 export: one `.fs` file for VDMX, Resolume Wire, the ISF Editor and other ISF hosts.

The file starts with the JSON comment the spec requires (hosts read the text between the first
`/*` and the first `*/`), followed by the shared header and the Shadertoy source behind a few
`#define`s. The host declares TIME, RENDERSIZE, isf_FragNormCoord and one uniform per INPUT, so the
file declares none of them. The isf-shim in `headless/harness.html` builds that host preamble from
the JSON, the way the VVISF reference implementation does, and renders the file.
"""

from __future__ import annotations

import json
import re

from .header import (CREDIT, build_header, display_name, effective_sliders, header_text,
                     licence_text, pascal)
from .td import channel_count, uses

TARGET_LABEL = "ISF 2.0 generator (VDMX, Resolume Wire, ISF Editor)"
STATUS_STEP = ("Experimental: this file is proven in a WebGL2 shim of an ISF host's wrapper (the "
               "VVISF reference implementation's OpenGL 3 / ES 3 path), not yet in VDMX or Wire. "
               "Compare the first frame with the reference before you rely on it.")

# Shadertoy uniform -> the ISF expression the host provides.
MAPPING = {
    "iResolution": "vec3(RENDERSIZE, 1.0)",
    "iTime": "TIME",
    "iTimeDelta": "TIMEDELTA",
    "iFrame": "FRAMEINDEX",
    "iDate": "DATE",
    "iMouse": "vec4(0.0)",
}
ISF_NAMES = frozenset("""TIME TIMEDELTA RENDERSIZE PASSINDEX FRAMEINDEX DATE isf_FragNormCoord
isf_FragColor gl_FragColor IMG_PIXEL IMG_NORM_PIXEL IMG_SIZE IMG_THIS_PIXEL IMG_THIS_NORM_PIXEL""".split())
_TEXTURE_CALL = re.compile(r"\btexture\s*\(\s*(iChannel\d+)\s*,")


def credit_text(record: dict, *, credit: bool = True) -> str:
    provenance = record.get("provenance") or {}
    parts = [f"Author: {header_text(provenance.get('author') or 'not stated', 80)}",
             f"Licence: {licence_text(record)}"]
    if provenance.get("url"):
        parts.append(f"Source: {header_text(provenance['url'])}")
    if credit:
        parts.append(CREDIT)
    return header_text("; ".join(parts), 400)


def metadata(record: dict, *, credit: bool = True) -> dict:
    inputs = []
    for slider in effective_sliders(record):
        inputs.append({"NAME": slider["name"], "TYPE": "float", "DEFAULT": float(slider["default"]),
                       "MIN": float(slider["min"]), "MAX": float(slider["max"]),
                       "LABEL": header_text(slider.get("label") or slider["name"], 60)})
    inputs += [{"NAME": f"iChannel{index}", "TYPE": "image", "LABEL": f"iChannel{index}"}
               for index in range(channel_count(record))]
    return {
        "ISFVSN": "2.0",
        "DESCRIPTION": header_text(f"{display_name(record)} ({header_text(record.get('id'), 80)}"
                                   f"@{header_text(record.get('version'), 12)}), a Shadertoy-style "
                                   "shader exported by Shaderbrücke", 300),
        "CREDIT": credit_text(record, credit=credit),
        "CATEGORIES": ["Generator"],
        "INPUTS": inputs,
    }


def source_with_images(record: dict) -> str:
    """`texture(iChannelN, uv)` becomes `IMG_NORM_PIXEL(iChannelN, uv)` when the record has
    channels; ISF hosts may hand an image over as a texture type `texture()` cannot sample."""
    source = (record.get("source") or "").strip("\n")
    if channel_count(record):
        source = _TEXTURE_CALL.sub(r"IMG_NORM_PIXEL(\1,", source)
    return source


def fragment(record: dict, *, credit: bool = True) -> str:
    source = record.get("source") or ""
    blob = json.dumps(metadata(record, credit=credit), indent=2, ensure_ascii=False)
    lines = [
        "/*" + blob + "*/",
        build_header(record, TARGET_LABEL, credit=credit).rstrip("\n"),
        "// The ISF host declares TIME, RENDERSIZE, isf_FragNormCoord and one uniform per INPUT;",
        "// do not declare them here. Shadertoy's names map onto them:",
        "",
    ]
    lines += [f"#define {name} {value}" for name, value in MAPPING.items() if uses(source, name)]
    lines += [
        "",
        source_with_images(record),
        "",
        "void main() {",
        "  vec4 color = vec4(0.0, 0.0, 0.0, 1.0);",
        "  // isf_FragNormCoord is the pixel centre in 0..1, so this is Shadertoy's fragCoord in pixels.",
        "  mainImage(color, isf_FragNormCoord * RENDERSIZE);",
        "  gl_FragColor = color;",
        "}",
        "",
    ]
    return "\n".join(lines)


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    return {f"{pascal(record.get('name'))}.fs": fragment(record, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    main = f"{pascal(record.get('name'))}.fs"
    source = record.get("source") or ""
    out = [
        STATUS_STEP,
        f"VDMX (Mac): copy {main} into ~/Library/Graphics/ISF (VDMX and the ISF Editor scan that "
        "folder and /Library/Graphics/ISF), restart VDMX and pick it from the Layer Source menu "
        "under the ISF sources; or drag the file onto a Media Bin page.",
        "Resolume Wire: add an ISF node, and under Attributes > Fragment Shader create a shader "
        "from the clipboard (paste the file) or browse for the .fs; the INPUTS appear as inlets.",
        "Each slider is a float INPUT whose DEFAULT is the tuned value, with MIN and MAX from its "
        "range; the host draws the controls. Never declare TIME, RENDERSIZE, isf_FragNormCoord or "
        "an INPUT as a uniform yourself: the host declares them and reports a redeclaration.",
        "Keep the JSON comment the first thing in the file and keep `*/` out of it: hosts read the "
        "JSON between the first `/*` and the first `*/`.",
        "The code is GLSL ES 3.00 (Shadertoy) style. The VVISF library compiles ISF as GLSL 3.30, "
        "4.00 or ES 3.00 on a modern context but as GLSL 1.10 on an OpenGL 2.1 context, where "
        "ES 3.00-only syntax (uint, bit operations, round, tanh, switch, texture()) fails.",
        "If the result looks darker in a host that composites with alpha, make sure the shader "
        "writes alpha 1.0 (the ISF spec's own note).",
        "Audio (audioFFT) inputs are not exported yet; audio-reactive ISF comes in a later version.",
    ]
    if uses(source, "iMouse"):
        out.append("iMouse is fixed at 0 here: ISF has no mouse uniform; add a point2D INPUT by "
                   "hand if you need one.")
    if uses(source, "iDate"):
        out.append("iDate maps to ISF's DATE; Shadertoy counts the month from 0, so check the "
                   "month if the shader reads it.")
    count = channel_count(record)
    if count:
        out.append(f"iChannel0..iChannel{count - 1} are image INPUTS read with IMG_NORM_PIXEL; "
                   "connect a source to each in the host.")
    clashes = [s["name"] for s in record.get("sliders") or [] if s["name"] in ISF_NAMES]
    if clashes:
        out.append(f"Rename the slider(s) {', '.join(clashes)}: the ISF host declares that name.")
    return out
