"""Export targets: one registry table every consumer reads.

Each row names its files, the integration steps, the proof method and its status. The
``shader_export`` schema enum, the Desktop export menu (M7) and the proof runner read this table,
so a new target is one row plus one generator module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import handoff, r3f, shadertoy, three, tsl


@dataclass(frozen=True)
class ExportTarget:
    id: str
    label: str
    main_file: str
    files: Callable[[dict, dict], dict[str, str]]
    steps: Callable[[dict, str | None], list[str]]
    proof: str
    status: str


EXPORT_TARGETS: dict[str, ExportTarget] = {
    "three": ExportTarget(
        id="three",
        label="three.js ShaderMaterial (three r187)",
        main_file="{name}Material.js",
        files=three.files,
        steps=three.steps,
        proof="three-harness",
        status="stable",
    ),
    "tsl": ExportTarget(
        id="tsl",
        label="TSL module (three r187 transpiler)",
        main_file="{name}.tsl.js",
        files=tsl.files,
        steps=tsl.steps,
        proof="tsl-harness",
        status="experimental",
    ),
    "r3f": ExportTarget(
        id="r3f",
        label="React Three Fiber component (@react-three/fiber 9, three r187)",
        main_file="{name}.tsx",
        files=r3f.files,
        steps=r3f.steps,
        proof="three-harness",
        status="stable",
    ),
    "agent": ExportTarget(
        id="agent",
        label="Agent handoff (one Markdown block)",
        main_file="{name}.handoff.md",
        files=handoff.files,
        steps=handoff.steps,
        proof="identity",
        status="stable",
    ),
    "shadertoy": ExportTarget(
        id="shadertoy",
        label="Shadertoy Image tab (paste-ready)",
        main_file="{name}.shadertoy.glsl",
        files=shadertoy.files,
        steps=shadertoy.steps,
        proof="identity",
        status="stable",
    ),
}


def target_ids() -> list[str]:
    return list(EXPORT_TARGETS)
