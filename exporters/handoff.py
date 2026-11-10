"""Agent handoff: one Markdown block another agent (or a teammate) can act on without the plugin.

The block carries the files (inline while they fit), where each goes, the uniform contract, the
host's pitfalls, provenance and licence, and an acceptance check that names a reference frame and
a measurable threshold. "Do not rewrite the shader maths" is the last line on purpose: the usual
failure is a second agent "improving" code it cannot see rendered.
"""

from __future__ import annotations

from . import three
from .header import build_header, code_fence, display_name, effective_sliders, header_text, pascal

INLINE_LIMIT = 12 * 1024      # bytes of file text; above this the block names paths instead
ACCEPT_LIMIT = 8.0            # channel difference per grid cell the acceptance check allows
REFERENCE_PNG = "reference-t1.png"


def _description(record: dict) -> str:
    name = display_name(record)
    sliders = effective_sliders(record)
    slider_text = (", ".join(f"{s['name']} ({s['min']:g}..{s['max']:g}, default {s['default']:g})"
                             for s in sliders) or "none")
    return (f"{name}: a Shadertoy-style GLSL fragment shader, compiled and frame-checked on the "
            f"server. Sliders: {slider_text}.")


def _contract_line(slider: dict) -> str:
    line = f"- `{slider['name']}`: slider, {slider['min']:g}..{slider['max']:g}, default {slider['default']:g}"
    if slider["default"] != float(slider["authored"]):
        line += f" (tuned by the human; authored {slider['authored']:g})"
    return line + "."


def _block(filename: str, text: str) -> list[str]:
    """One file as a fenced block whose fence no backtick run in the file can close."""
    body = text.rstrip("\n")
    opening, closing = code_fence(body, "glsl" if filename.endswith(".glsl") else "js")
    return [f"### {filename}", opening, body, closing, ""]


def handoff(record: dict, *, credit: bool = True, acceptance: dict | None = None) -> str:
    """The Markdown block. `acceptance` carries the measured reference grid
    (``{"grid": [[r, g, b] x 9], "png": "reference-t1.png"}``); without it the check names the
    reference frame and says where the numbers come from."""
    name = pascal(record.get("name"))
    files = three.files(record, {"credit": credit})
    total = sum(len(text.encode("utf-8")) for text in files.values())
    inline = total <= INLINE_LIMIT
    header = build_header(record, "agent handoff (three.js ShaderMaterial, three r187)", credit=credit)
    # The material module embeds the fragment, so it is the one file that must always travel with
    # the block: a receiving agent without this plugin still has everything it needs.
    primary = next((filename for filename in files if filename.endswith("Material.js")),
                   next(iter(files)))
    lines = [
        f"# Shaderbrücke handoff: {display_name(record)} "
        f"({header_text(record.get('id'), 80)}@{header_text(record.get('version'), 12)})",
        "",
        _description(record),
        "",
        "## Target host",
        "three.js r187 or newer, GLSL3 (`ShaderMaterial` with `glslVersion: THREE.GLSL3`). "
        "The R3F component uses `@react-three/fiber` 9.",
        "",
        "## Files",
        f"`{primary}` is inline below: it embeds the fragment, so it is everything you need."
        + ("" if inline else
           f" The other files ({', '.join(name for name in files if name != primary)}) are optional "
           "reference copies; ask for them if you want the standalone fragment."),
        "",
        *_block(primary, files[primary]),
    ]
    if inline:
        for filename, text in files.items():
            if filename == primary:
                continue
            lines += _block(filename, text)
    lines += [
        "## Where each file goes",
        f"- `{name}Material.js` -> your project's shader folder (e.g. `src/shaders/`).",
        f"- `{name}.frag.glsl` -> the same folder, for reading and diffs; the factory embeds it.",
        "",
        "## Copy-paste",
        "Plain three.js:",
        "```js",
        f"import {{ create{name}Material }} from './shaders/{name}Material.js'",
        f"const material = create{name}Material()",
        "material.setSize(window.innerWidth, window.innerHeight)",
        "// each frame: material.update(elapsedSeconds)",
        "```",
        "React Three Fiber (inside an existing `<Canvas>`):",
        "```tsx",
        f"import {{ {name}Background }} from './shaders/{name}'",
        f"<{name}Background />",
        "```",
        "",
        "## Integration steps",
        *[f"{index + 1}. {step}" for index, step in enumerate(three.steps(record, None))],
        "",
        "## Uniform contract",
        "- `iTime`: seconds, float. Advance it every frame.",
        "- `iResolution`: pixels, vec2/vec3. Set it on resize.",
        "- `fragCoord`: pixels, origin bottom left (GLSL convention).",
        *[_contract_line(s) for s in effective_sliders(record)],
        "",
        "## Known pitfalls for this host",
        "- The material has `depthTest`/`depthWrite` off: it expects to cover the viewport.",
        "- A full-screen quad with a big triangle (clip-space positions `(-1,-1) (3,-1) (-1,3)`) "
        "avoids the diagonal seam of two triangles.",
        "- `ShaderMaterial` cannot be shared across renderers; create one per context.",
        "",
        "## Provenance and licence",
        *[f"- {line}" for line in header.strip().splitlines()[3:]],
        "",
        "## Acceptance check",
        *_acceptance(acceptance),
        "",
        "Do not rewrite the shader maths; change slider values instead.",
        "",
    ]
    return "\n".join(lines)


CELLS = ("top left", "top centre", "top right", "middle left", "centre", "middle right",
         "bottom left", "bottom centre", "bottom right")


def _acceptance(acceptance: dict | None) -> list[str]:
    """A check any agent can run on its own screenshot: no path on this host, only numbers."""
    how = (f"Render one frame at t = 1 s, 512x288, with the slider defaults above. Split it into "
           f"a 3x3 grid of equal cells and take the mean R, G and B (0 to 255) of each cell, row "
           f"by row from the top left.")
    grid = (acceptance or {}).get("grid")
    if not (isinstance(grid, list) and len(grid) == 9):
        return [how,
                f"The reference frame is `{REFERENCE_PNG}`. This copy of the handoff carries no "
                "measured numbers: ask for the export from `shader_export` (target `agent`), "
                "which ships the frame and the grid next to this file.",
                f"A mean channel difference above {ACCEPT_LIMIT:g}/255 means it is not the same "
                "shader."]
    png = (acceptance or {}).get("png") or REFERENCE_PNG
    rows = [f"| {index + 1} | {CELLS[index]} | {cell[0]:.1f} | {cell[1]:.1f} | {cell[2]:.1f} |"
            for index, cell in enumerate(grid)]
    return [how,
            f"`{png}` next to this file is the reference frame those numbers come from.",
            "",
            "| # | cell | R | G | B |",
            "|---|---|---|---|---|",
            *rows,
            "",
            f"Every channel of every cell within {ACCEPT_LIMIT:g}/255 of these numbers means it is "
            "the same shader. A larger difference means it is not: report the numbers you measured "
            "instead of tuning the code by hand."]


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {f"{name}.handoff.md": handoff(record, credit=credit,
                                          acceptance=options.get("acceptance"))}


def steps(record: dict, dest: str | None) -> list[str]:
    return [
        "Paste the whole handoff block into the other agent's session (or save it as a file and "
        "point the agent at it).",
        "The receiving agent needs file-writing tools; it does not need this plugin.",
        "Ask it to run the acceptance check at the end and report the measured difference.",
    ]
