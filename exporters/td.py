"""TouchDesigner GLSL TOP export: one pixel shader for a Text DAT, plus the uniforms to bind.

TouchDesigner adds `#version`, `vUV`, `uTDOutputInfo`, `TDOutputSwizzle` and the input samplers
itself, so the file declares only what the user binds on the GLSL TOP's Vectors page. `bindings()`
is the one table the file comments and the steps both read, and the td-shim in
`headless/harness.html` parses those comments: the values the proof renders with are the values
the steps tell the user to type.
"""

from __future__ import annotations

import re

from .header import build_header, effective_sliders, header_text, pascal

TARGET_LABEL = "TouchDesigner GLSL TOP pixel shader"
STATUS_STEP = ("Experimental: this file is proven in a WebGL2 shim of the GLSL TOP's wrapper, not "
               "yet inside TouchDesigner itself (GLSL 4.60 on Vulkan). Compare the first frame "
               "with the reference before you rely on it.")

# Shadertoy uniform -> (declared name, GLSL type, Vectors page value, what the value means).
# `iFrame` is an int in Shadertoy but the Vectors page sends floats, so it arrives as `sbFrame`.
CLOCK = {
    "iTime": ("iTime", "float", "absTime.seconds",
              "seconds since TouchDesigner started; `me.time.seconds` follows the timeline instead"),
    "iTimeDelta": ("iTimeDelta", "float", "absTime.stepSeconds", "seconds per frame"),
    "iFrame": ("sbFrame", "float", "absTime.frame", "the frame counter; the shader reads it as int"),
}
OPTIONAL = {
    "iMouse": ("iMouse", "vec4", "0 0 0 0",
               "optional; Shadertoy's value before a click, bind x and y in pixels to move it"),
    "iDate": ("iDate", "vec4", "0 0 0 0",
              "optional; Shadertoy's order is year, month counted from 0, day, seconds of the day"),
}
# Names TouchDesigner declares in every GLSL TOP, or that this file declares itself.
TD_NAMES = re.compile(r"^(?:uTD|sTD|TD|vUV$|sbFrame$)")


def uses(source: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", source) is not None


def channel_count(record: dict) -> int:
    return len(record.get("channels") or [])


def bindings(record: dict) -> list[dict]:
    """Every uniform the user binds on the Vectors page, in file order."""
    source = record.get("source") or ""
    rows = []
    for shadertoy_name, (name, kind, value, note) in {**CLOCK, **OPTIONAL}.items():
        if uses(source, shadertoy_name):
            rows.append({"name": name, "type": kind, "value": value, "note": note,
                         "for": shadertoy_name})
    for slider in effective_sliders(record):
        note = f"range {slider['min']:g}..{slider['max']:g}"
        if slider.get("step"):
            note += f", step {slider['step']:g}"
        if slider["default"] != float(slider["authored"]):
            note += f"; tuned, authored {slider['authored']:g}"
        rows.append({"name": slider["name"], "type": "float", "value": f"{slider['default']:g}",
                     "note": note, "for": "slider"})
    return rows


def pixel_shader(record: dict, *, credit: bool = True) -> str:
    header = build_header(record, TARGET_LABEL, credit=credit)
    lines = [
        header.rstrip("\n"),
        "// TouchDesigner declares vUV, uTDOutputInfo and TDOutputSwizzle and adds #version; do not",
        "// declare them. Bind each uniform below on the GLSL TOP's Vectors page (Name, Value):",
        "// an unbound uniform reads 0.",
        "",
    ]
    for row in bindings(record):
        lines.append(f"uniform {row['type']} {row['name']};  // Vectors page: {row['name']} = "
                     f"{row['value']} ({header_text(row['note'])})")
    lines.append("#define iResolution vec3(uTDOutputInfo.res.zw, 1.0)")
    if uses(record.get("source") or "", "iFrame"):
        lines.append("#define iFrame int(sbFrame)")
    count = channel_count(record)
    if count:
        lines.append("// TouchDesigner and Shadertoy (with its default V-flip) both put uv (0, 0) at "
                     "an image's bottom left,")
        lines.append("// so the inputs are sampled as they are.")
        lines += [f"#define iChannel{index} sTD2DInputs[{index}]" for index in range(count)]
    lines += [
        "",
        "layout(location = 0) out vec4 fragColor;",
        "",
        (record.get("source") or "").strip("\n"),
        "",
        "void main() {",
        "  vec4 color = vec4(0.0, 0.0, 0.0, 1.0);",
        "  // vUV is the pixel centre in 0..1, so this is Shadertoy's fragCoord in pixels.",
        "  mainImage(color, vUV.st * uTDOutputInfo.res.zw);",
        "  fragColor = TDOutputSwizzle(color);",
        "}",
        "",
    ]
    return "\n".join(lines)


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    return {f"{pascal(record.get('name'))}.td.glsl": pixel_shader(record, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    main = f"{pascal(record.get('name'))}.td.glsl"
    out = [
        STATUS_STEP,
        f"In TouchDesigner add a GLSL TOP and paste {main} into its docked pixel shader Text DAT "
        "(glsl1_pixel), or point the GLSL page's Pixel Shader parameter at a Text DAT holding it.",
        "On the Common page set Output Resolution to Custom Resolution and enter the size you "
        "want; without an input the TOP does not know your output size. Keep Pixel Format at "
        "8-bit fixed (RGBA) or a float format: sRGB 8-bit fixed re-encodes colours the shader "
        "already writes gamma-encoded.",
    ]
    for row in bindings(record):
        if row["for"] == "slider":
            out.append(f"Vectors page: add a row with Name `{row['name']}` and Value `{row['value']}` "
                       f"({row['note']}). For a slider, add a custom Float parameter on the parent "
                       f"COMP and set the Value to an expression like `parent().par.{row['name'].capitalize()}`.")
        elif row["type"] == "vec4":
            out.append(f"Vectors page: Name `{row['name']}`, Values `{row['value']}` ({row['note']}).")
        else:
            out.append(f"Vectors page: Name `{row['name']}`, Value 0 as the expression "
                       f"`{row['value']}` ({row['note']}).")
    out += [
        "Press Load Uniform Names on the GLSL page to check the names; the compiler drops a "
        "uniform the shader never reads, so a missing name there is not an error.",
        "A checkerboard means the shader did not compile: read the compiler log in the GLSL TOP's "
        "info DAT. TouchDesigner adds #version itself, so never add one to the file.",
    ]
    count = channel_count(record)
    if count:
        out.append(f"Connect the image for iChannel0..iChannel{count - 1} to the GLSL TOP's inputs "
                   "in that order (2D inputs are counted in order). No flip is needed unless the "
                   "Shadertoy channel had V-flip turned off.")
    clashes = [s["name"] for s in record.get("sliders") or [] if TD_NAMES.match(s["name"])]
    if clashes:
        out.append(f"Rename the slider(s) {', '.join(clashes)}: TouchDesigner reserves names that "
                   "start with uTD, sTD or TD, and vUV.")
    return out
