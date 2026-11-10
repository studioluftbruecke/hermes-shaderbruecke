"""three.js `ShaderMaterial` export: a standalone `.frag.glsl` and a material factory module.

The fragment is self-contained (`#version 300 es` comes from `glslVersion: THREE.GLSL3`, so the
file starts with precision and its own `out vec4`). The material factory embeds that same text, so
a bundler needs no GLSL loader, and the proof harness imports the factory itself: what it renders
is the file the user gets.
"""

from __future__ import annotations

from .header import build_header, display_name, effective_sliders, js_template_literal, pascal

# three's ShaderMaterial prefix already declares the built-in attributes and adds `#version`
# for GLSL3, so this is just the body.
VERTEX = """const vertexShader = `
void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }
`
"""


def fragment(record: dict, *, credit: bool = True) -> str:
    record_sliders = record.get("sliders") or []
    header = build_header(record, "three.js ShaderMaterial (three r187)", credit=credit)
    uniforms = ["uniform vec3 iResolution;", "uniform float iTime;", "uniform float iTimeDelta;",
                "uniform int iFrame;", "uniform vec4 iMouse;", "uniform vec4 iDate;"]
    uniforms += [f"uniform float {slider['name']};" for slider in record_sliders]
    return "\n".join([
        header.rstrip("\n"),
        "",
        "precision highp float;",
        "precision highp int;",
        *uniforms,
        "out vec4 fragColor;",
        "",
        (record.get("source") or "").strip("\n"),
        "",
        "void main() {",
        "  vec4 color = vec4(0.0, 0.0, 0.0, 1.0);",
        "  mainImage(color, gl_FragCoord.xy);",
        "  fragColor = color;",
        "}",
        "",
    ])


def material(record: dict, *, credit: bool = True) -> str:
    name = pascal(record.get("name"))
    sliders = effective_sliders(record)
    header = build_header(record, "three.js ShaderMaterial (three r187)", credit=credit)
    fragment_text = js_template_literal(fragment(record, credit=credit))
    slider_uniforms = "\n".join(
        f"    {slider['name']}: {{ value: options.{slider['name']} ?? {slider['default']:g} }},"
        for slider in sliders)
    slider_params = ", ".join(f"{slider['name']}" for slider in sliders)
    options_line = f" * @param {{{{{slider_params}}}}} [options]" if sliders else ""
    return f"""{header.strip()}

import * as THREE from 'three'

/** The shader's fragment source. Edit the uniforms, not this string. */
export const fragmentShader = `{fragment_text}`

{VERTEX}
/**
 * Create the {display_name(record)} material.
{options_line}
 * @returns {{THREE.ShaderMaterial}} call `setSize(width, height)` and `update(seconds)` each frame
 */
export function create{name}Material(options = {{}}) {{
  const uniforms = {{
    iResolution: {{ value: new THREE.Vector3(1, 1, 1) }},
    iTime: {{ value: 0 }},
    iTimeDelta: {{ value: 1 / 60 }},
    iFrame: {{ value: 0 }},
    iMouse: {{ value: new THREE.Vector4(0, 0, 0, 0) }},
    iDate: {{ value: new THREE.Vector4() }},
{slider_uniforms}
  }}
  const material = new THREE.ShaderMaterial({{
    glslVersion: THREE.GLSL3,
    uniforms,
    vertexShader,
    fragmentShader,
    depthTest: false,
    depthWrite: false
  }})
  material.setSize = (width, height) => uniforms.iResolution.value.set(width, height, 1)
  material.update = (seconds) => {{
    uniforms.iTime.value = seconds
    uniforms.iFrame.value = Math.round(seconds * 60)
    uniforms.iTimeDelta.value = 1 / 60
  }}
  return material
}}
"""


def files(record: dict, options: dict) -> dict[str, str]:
    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {
        f"{name}Material.js": material(record, credit=credit),
        f"{name}.frag.glsl": fragment(record, credit=credit),
    }


def steps(record: dict, dest: str | None) -> list[str]:
    name = pascal(record.get("name"))
    return [
        f"Copy {name}Material.js into your project (for example src/shaders/).",
        f"Import it: `import {{ create{name}Material }} from './shaders/{name}Material.js'`.",
        "Your three.js version must be r187 or newer; the material is GLSL3 (`glslVersion: THREE.GLSL3`).",
        f"Create it once: `const material = create{name}Material({{ {', '.join(s['name'] for s in record.get('sliders') or []) or ''} }})`.",
        "Put it on a plane or a full-screen quad, call `material.setSize(w, h)` on resize and "
        "`material.update(elapsedSeconds)` in your render loop.",
        "The material has depthTest and depthWrite off: it expects to cover the viewport.",
    ]
