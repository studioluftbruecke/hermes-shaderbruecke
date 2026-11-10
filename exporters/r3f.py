"""React Three Fiber export: one `.tsx` component that uses the three.js material factory."""

from __future__ import annotations

from .header import build_header, display_name, effective_sliders, pascal


def component(record: dict, *, credit: bool = True) -> str:
    name = pascal(record.get("name"))
    sliders = effective_sliders(record)
    header = build_header(record, "React Three Fiber (@react-three/fiber 9, three r187)", credit=credit)
    if sliders:
        props = "\n".join(f"  {slider['name']}?: number" for slider in sliders)
        defaults = "\n".join(
            f"  {slider['name']} = {slider['default']:g}," for slider in sliders)
        destructure = ", ".join(f"{slider['name']} = {slider['default']:g}" for slider in sliders)
        factory_args = ", ".join(f"{slider['name']}" for slider in sliders)
        deps = ", ".join(f"{slider['name']}" for slider in sliders)
    else:
        props = "  // this shader has no sliders"
        defaults = ""
        destructure = ""
        factory_args = ""
        deps = ""
    return f"""{header.strip()}

import {{ useMemo, useRef }} from 'react'
import {{ Canvas, useFrame, useThree }} from '@react-three/fiber'
import type {{ ThreeElements }} from '@react-three/fiber'
import * as THREE from 'three'
import {{ create{name}Material }} from './{name}Material.js'

export type {name}Props = {{
{props}
}}

export function {name}Background({{ {destructure} }}: {name}Props) {{
  const material = useMemo(
    () => create{name}Material({{ {factory_args} }}),
    [{deps}]
  )
  const size = useThree((state) => state.size)

  material.setSize(size.width, size.height)

  useFrame((state) => material.update(state.clock.elapsedTime))

  return (
    <mesh>
      <planeGeometry args={{[2, 2]}} />
      <primitive object={{material}} attach="material" />
    </mesh>
  )
}}

/** Full-screen {display_name(record)} background for an existing Canvas: `<{name}Background />`. */
export function {name}(props: {name}Props = {{}}) {{
  return (
    <Canvas orthographic camera={{{{ position: [0, 0, 1], left: -1, right: 1, top: 1, bottom: -1, near: 0.1, far: 10 }}}}>
      <{name}Background {{...props}} />
    </Canvas>
  )
}}
"""


def files(record: dict, options: dict) -> dict[str, str]:
    """The component plus the material module it imports: the pair is what a user copies."""
    from . import three

    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {f"{name}.tsx": component(record, credit=credit),
            f"{name}Material.js": three.material(record, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    name = pascal(record.get("name"))
    sliders = record.get("sliders") or []
    return [
        f"Copy {name}.tsx and {name}Material.js into the same folder of your app.",
        "Install three r187 and @react-three/fiber 9 if the project does not have them yet: "
        "`npm install three@0.187.0 @react-three/fiber@9`.",
        f"Own canvas: render `<{name} />`. Inside an existing Canvas: render `<{name}Background />` "
        "and keep your own camera.",
        ("Tune it with props: " + ", ".join(s["name"] for s in sliders) + ".") if sliders else
        "This shader takes no props.",
        "The component sets the canvas size and the time uniform for you; do not add your own "
        "`update` loop for it.",
    ]
