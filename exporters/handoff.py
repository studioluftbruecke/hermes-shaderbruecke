"""Agent handoff: one Markdown block another agent (or a teammate) can act on without the plugin.

The block carries the files (inline while they fit), where each goes, the uniform contract, the
host's pitfalls, provenance and licence, and an acceptance check that names a reference frame and
a measurable threshold. "Do not rewrite the shader maths" is the last line on purpose: the usual
failure is a second agent "improving" code it cannot see rendered.
"""

from __future__ import annotations

from . import three
from .header import build_header, pascal

INLINE_LIMIT = 12 * 1024      # bytes of file text; above this the block names paths instead
ACCEPT_LIMIT = 8.0            # mean channel difference the acceptance check allows


def _description(record: dict) -> str:
    name = record.get("name") or "shader"
    sliders = record.get("sliders") or []
    slider_text = (", ".join(f"{s['name']} ({s['min']:g}..{s['max']:g}, default {s['default']:g})"
                             for s in sliders) or "none")
    return (f"{name}: a Shadertoy-style GLSL fragment shader, compiled and frame-checked on the "
            f"server. Sliders: {slider_text}.")


def _reference(record: dict) -> str:
    files = record.get("files") or {}
    return files.get("snapshot") or files.get("contact_sheet") or "(no reference frame stored)"


def handoff(record: dict, *, credit: bool = True) -> str:
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
        f"# Shaderbrücke handoff: {record.get('name')} ({record.get('id')}@{record.get('version')})",
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
        f"### {primary}",
        "```" + ("glsl" if primary.endswith(".glsl") else "js"),
        files[primary].rstrip("\n"), "```",
        "",
    ]
    if inline:
        for filename, text in files.items():
            if filename == primary:
                continue
            lines += [f"### {filename}", "```" + ("glsl" if filename.endswith(".glsl") else "js"),
                      text.rstrip("\n"), "```", ""]
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
        *[f"- `{s['name']}`: slider, {s['min']:g}..{s['max']:g}, default {s['default']:g}." for s in
          record.get("sliders") or []],
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
        f"Render the first frame at t = 1 s with the default slider values and compare it with "
        f"`{_reference(record)}`: same composition and colours. A mean channel difference above "
        f"{ACCEPT_LIMIT:g}/255 means it is not the same shader.",
        "",
        "Do not rewrite the shader maths; change slider values instead.",
        "",
    ]
    return "\n".join(lines)


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {f"{name}.handoff.md": handoff(record, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    return [
        "Paste the whole handoff block into the other agent's session (or save it as a file and "
        "point the agent at it).",
        "The receiving agent needs file-writing tools; it does not need this plugin.",
        "Ask it to run the acceptance check at the end and report the measured difference.",
    ]
