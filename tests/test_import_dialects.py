"""Pasted shaders: Shadertoy, glslCanvas and ISF are detected and normalised to the internal form
(`mainImage`, Shadertoy uniforms, `@slider` lines from ISF float inputs); multipass, textures and
audio are refused with the reason."""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import checks, library, record  # noqa: E402

SHADERTOY = """void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    fragColor = vec4(uv, 0.5 + 0.5 * sin(iTime), 1.0);
}
"""

GLSLCANVAS = """#ifdef GL_ES
precision mediump float;
#endif
uniform vec2 u_resolution;
uniform float u_time;
uniform vec2 u_mouse;

void main() {
    vec2 st = gl_FragCoord.xy / u_resolution;
    gl_FragColor = vec4(st.x, st.y, abs(sin(u_time)), 1.0);
}
"""

ISF = """/*{
    "DESCRIPTION": "Soft rings",
    "CREDIT": "by demo",
    "ISFVSN": "2.0",
    "CATEGORIES": ["Generator"],
    "INPUTS": [
        {"NAME": "rings", "TYPE": "float", "DEFAULT": 6.0, "MIN": 1.0, "MAX": 20.0, "LABEL": "Rings"},
        {"NAME": "invert", "TYPE": "bool", "DEFAULT": false},
        {"NAME": "tint", "TYPE": "color", "DEFAULT": [1.0, 0.5, 0.2, 1.0]}
    ]
}*/
void main() {
    vec2 p = isf_FragNormCoord - 0.5;
    p.x *= RENDERSIZE.x / RENDERSIZE.y;
    float v = 0.5 + 0.5 * sin(length(p) * rings * 6.2831 - TIME);
    if (invert) v = 1.0 - v;
    gl_FragColor = vec4(v * tint.rgb, 1.0);
}
"""


@pytest.mark.parametrize("source, dialect", [(SHADERTOY, "shadertoy"), (GLSLCANVAS, "glslcanvas"),
                                             (ISF, "isf"), ("float x;", "unknown")])
def test_dialects_are_detected(source, dialect):
    assert library.detect_dialect(source) == dialect


def lint_errors(source):
    sliders = record.parse_sliders(source)
    return [f for f in checks.lint(source, slider_names=[s["name"] for s in sliders]) if f.severity == "error"]


def test_shadertoy_passes_through_and_lints_clean():
    normal = library.normalise_pasted(SHADERTOY)
    assert normal["refused"] is None and normal["source"].strip() == SHADERTOY.strip()
    assert lint_errors(normal["source"]) == []


def test_glslcanvas_becomes_main_image_with_shadertoy_uniforms():
    normal = library.normalise_pasted(GLSLCANVAS)
    body = normal["source"]
    assert normal["refused"] is None
    assert "void mainImage(out vec4 fragColor, in vec2 fragCoord)" in body
    assert "u_time" not in body and "iTime" in body and "iResolution.xy" in body
    assert "gl_FragColor" not in body and "fragCoord / iResolution.xy" in body
    assert "precision" not in body and "#ifdef" not in body
    assert lint_errors(body) == []


def test_isf_float_inputs_become_sliders_and_the_rest_constants():
    normal = library.normalise_pasted(ISF)
    body = normal["source"]
    assert normal["refused"] is None and normal["name"] == "Soft rings" and normal["author"] == "by demo"
    assert record.parse_sliders(body) == [{"name": "rings", "min": 1.0, "max": 20.0, "default": 6.0,
                                           "step": None, "label": "Rings"}]
    assert "const bool invert = false;" in body and "const vec4 tint = vec4(1, 0.5, 0.2, 1);" in body
    assert "isf_FragNormCoord" not in body and "(fragCoord / iResolution.xy)" in body
    assert "TIME" not in body.replace("iTime", "") and "gl_FragColor" not in body
    assert len(normal["warnings"]) == 2
    assert lint_errors(body) == []


@pytest.mark.parametrize("source, reason", [
    (SHADERTOY.replace("vec4(uv,", "texture(iChannel0, uv) + vec4(uv,"), "iChannel"),
    (ISF.replace('"INPUTS"', '"PASSES": [{"TARGET": "a"}], "INPUTS"'), "multipass"),
    (ISF.replace('"TYPE": "bool"', '"TYPE": "image"'), "image and audio inputs"),
    (ISF.replace("float v =", "vec4 c = IMG_NORM_PIXEL(inputImage, isf_FragNormCoord);\n    float v ="), "samples images"),
    (GLSLCANVAS.replace("uniform vec2 u_mouse;", "uniform sampler2D u_tex;"), "textures"),
    (GLSLCANVAS.replace("uniform vec2 u_mouse;", "uniform float u_speed;"), "u_speed"),
    ("float nothing;", "not a Shadertoy"),
])
def test_unsupported_shaders_are_refused_with_the_reason(source, reason):
    normal = library.normalise_pasted(source)
    assert normal["source"] is None and reason in normal["refused"]


@pytest.mark.parametrize("header, reason", [
    ('{"ISFVSN": "2", "INPUTS": [{"NAME": "amt", "TYPE": "float", "MIN": "low"}]}', "amt"),
    ('{"ISFVSN": "2", "INPUTS": [{"NAME": "tint", "TYPE": "color", "DEFAULT": "red"}]}', "tint"),
    ('{"ISFVSN": "2", "INPUTS": [{"NAME": "spot", "TYPE": "point2D", "DEFAULT": [null, 1]}]}', "spot"),
    ('{"ISFVSN": "2", "INPUTS": [{"NAME": "n", "TYPE": "long", "DEFAULT": "many"}]}', "n"),
    ('{"ISFVSN": "2", "INPUTS": 5}', "INPUTS"),
])
def test_a_malformed_isf_header_is_refused_not_raised(header, reason):
    normal = library.normalise_pasted(f"/*{header}*/\nvoid main() {{ gl_FragColor = vec4(TIME); }}\n")
    assert normal["source"] is None and normal["refused"] and reason in normal["refused"]


def test_an_isf_label_cannot_add_lines_to_the_source():
    header = '{"ISFVSN": "2", "INPUTS": [{"NAME": "amt", "TYPE": "float", "LABEL": "x\\nfloat evil = 1.0;"}]}'
    body = library.normalise_pasted(f"/*{header}*/\nvoid main() {{ gl_FragColor = vec4(amt); }}\n")["source"]
    assert "\nfloat evil" not in body
    assert record.parse_sliders(body)[0]["name"] == "amt"
