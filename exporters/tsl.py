"""TSL export: three r187's own transpiler, rewritten into a module that actually runs.

The transpiler is JavaScript inside the vendored build, so the raw text comes from the harness
(`transpileRaw`); every rewrite after it happens here, once, in Python, so the export and the proof
share one implementation and a unit test can hold it.

The four rewrites (measured in probe P8, `probes/RESULTS.md` §TSL emitter defects):

1. `fragCoord` is a vec3 whose z is `screenCoordinate.z`, which is NaN in a node material; a
   Shadertoy Image pass has a vec2, and the vec3/vec2 mismatch poisons every `length()`.
2. `exp( - 26.0.mul( d ) )` puts a unary minus on a number literal's method call: a TypeError at
   build time.
3. A GLSL local written after its declaration must be a `.toVar()`; an assign to a const node
   breaks the graph silently.
4. The import list is rebuilt last, because the rewrites can introduce `float()` or `vec2()`.
"""

from __future__ import annotations

import re

from .header import build_header, pascal

FORBIDDEN_NAMES = ("iMouse", "iFrame", "iDate", "iTimeDelta", "iChannel")

MAT2_RE = re.compile(r"\bmat2\(\s*([^()]*?)\s*\)")
MAT3_RE = re.compile(r"\bmat3\(\s*([^()]*?)\s*\)")


def _split_args(text: str) -> list[str]:
    args, depth, current = [], 0, ""
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += char
    args.append(current.strip())
    return [arg for arg in args if arg != ""]


def _transpose(args: list[str], size: int) -> list[str]:
    """GLSL passes columns, three's TSL constructor fills rows: transposing the flat list fixes it."""
    transposed = [None] * (size * size)
    for row in range(size):
        for column in range(size):
            transposed[row * size + column] = args[column * size + row]
    return transposed  # type: ignore[return-value]


def transpose_matrix_constructors(text: str) -> str:
    """`mat2(a, b, c, d)` -> `mat2(a, c, b, d)`, and the mat3 equivalent.

    Measured on the vendored three r187: the transpiled module renders a rotated pattern 177/255
    away from the source until this is applied, then 0/255. GLSL's constructor takes columns,
    three's Matrix2/Matrix3 `.set()` takes the same numbers as rows, so the matrix arrives
    transposed (a rotation becomes its inverse). Non-square argument lists (a diagonal `mat2(0.5)`,
    two vec2 columns, three mat3 columns) are left alone.
    """
    def fix2(match: re.Match) -> str:
        args = _split_args(match.group(1))
        return "mat2( " + ", ".join(_transpose(args, 2)) + " )" if len(args) == 4 else match.group(0)

    def fix3(match: re.Match) -> str:
        args = _split_args(match.group(1))
        return "mat3( " + ", ".join(_transpose(args, 3)) + " )" if len(args) == 9 else match.group(0)

    return MAT3_RE.sub(fix3, MAT2_RE.sub(fix2, text))


MATERIAL_NODE = """
// Entry point: wire this into a node material, e.g.
//   const material = new MeshBasicNodeMaterial()
//   material.colorNode = materialNode()
export const materialNode = () => mainImage().rgb
"""

_FRAGCOORD_RAW = ("const fragCoord = vec3( screenCoordinate.x, screenSize.y.sub( screenCoordinate.y ), "
                  "screenCoordinate.z );")
_FRAGCOORD_FIX = "const fragCoord = vec2( screenCoordinate.x, screenSize.y.sub( screenCoordinate.y ) );"

_IMPORT_RE = re.compile(r"^import \{([^}]*)\} from 'three/tsl';$", re.MULTILINE)
_ASSIGN_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\.(?:assign|addAssign|subAssign|mulAssign|divAssign)\(")
_DECL_RE = re.compile(r"^(\s*)const ([A-Za-z_$][\w$]*) = (.*);$", re.MULTILINE)
_NEGATED_LITERAL_RE = re.compile(r"(^|[\s(,])-\s(\d+(?:\.\d+)?)\.([A-Za-z_$][\w$]*)\(")


def guard(record: dict) -> str | None:
    """Why this shader cannot become a TSL module, or None when it can."""
    source = record.get("source") or ""
    if not re.search(r"mainImage\s*\(\s*out\s+\w+\s+fragColor\s*,\s*in\s+\w+\s+fragCoord\s*\)", source):
        return ("the TSL decoder needs `void mainImage(out vec4 fragColor, in vec2 fragCoord)`; "
                "rename the parameters to fragColor and fragCoord")
    body = re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.DOTALL)
    for name in FORBIDDEN_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", body):
            return (f"`{name}` has no TSL equivalent in a single-pass Image shader; remove it or use "
                    "iTime and iResolution only")
    if re.search(r"iResolution\s*\.\s*z\b", body):
        return "`iResolution.z` does not exist in the TSL decode (screenSize is a vec2); use iResolution.xy"
    return None


def _fix_imports(text: str) -> str:
    def rebuild(match: re.Match) -> str:
        names = [part.strip() for part in match.group(1).split(",") if part.strip()]
        for needed in ("uniform", "time", "vec2", "vec3", "float"):
            if needed not in names and re.search(rf"(^|[^\w$.]){needed}\(", text):
                names.append(needed)
        return "import { " + ", ".join(names) + " } from 'three/tsl';"

    return _IMPORT_RE.sub(rebuild, text)


def rewrite(raw: str, sliders: list[dict] | None = None) -> str:
    """The raw transpiler output, made runnable: slider uniforms exported with defaults, iTime bound."""
    out = raw.replace(_FRAGCOORD_RAW, _FRAGCOORD_FIX)
    if "const iTime = time;" in out:
        out = out.replace("const iTime = time;",
                          "export const sbTime = uniform( 0.0 );\nconst iTime = sbTime;")
    for slider in sliders or []:
        out = out.replace(
            f"const {slider['name']} = uniform( 'float' );",
            f"export const {slider['name']} = uniform( {slider['default']:g} );")
    out = _NEGATED_LITERAL_RE.sub(lambda m: f"{m.group(1)}float( -{m.group(2)} ).{m.group(3)}(", out)

    assigned = set(_ASSIGN_RE.findall(out))

    def declare(match: re.Match) -> str:
        indent, name, value = match.group(1), match.group(2), match.group(3)
        if name not in assigned or ".toVar()" in value or value.startswith("property("):
            return match.group(0)
        return f"{indent}const {name} = {value}.toVar();"

    out = _DECL_RE.sub(declare, out)
    out = transpose_matrix_constructors(out)
    out = _fix_imports(out)
    if "export const materialNode" not in out:
        out = out.rstrip("\n") + "\n" + MATERIAL_NODE
    return out


def module(record: dict, raw: str, *, credit: bool = True) -> str:
    header = build_header(record, "TSL module (three r187 transpiler)", credit=credit)
    return header + "\n" + rewrite(raw, record.get("sliders") or [])


def files(record: dict, options: dict) -> dict[str, str]:
    raw = options.get("raw")
    if raw is None:
        raise ValueError("the tsl target needs the raw transpiler output (options['raw'])")
    credit = bool(options.get("credit", True))
    name = pascal(record.get("name"))
    return {f"{name}.tsl.js": module(record, raw, credit=credit)}


def steps(record: dict, dest: str | None) -> list[str]:
    name = pascal(record.get("name"))
    sliders = record.get("sliders") or []
    return [
        f"Copy {name}.tsl.js into your project and import {{ materialNode, sbTime"
        + (", " + ", ".join(s["name"] for s in sliders) if sliders else "") + " } from it.",
        "three r187 or newer; TSL is `three/tsl` and the renderer is `three/webgpu`.",
        "Build a node material: `const material = new MeshBasicNodeMaterial(); "
        "material.colorNode = materialNode()`.",
        "Set `sbTime.value = elapsedSeconds` each frame; slider uniforms are exported with their "
        "defaults (`<name>.value = ...`).",
        "TSL is experimental in three r187: read the proof verdict, and do not rewrite the module by hand.",
    ]
