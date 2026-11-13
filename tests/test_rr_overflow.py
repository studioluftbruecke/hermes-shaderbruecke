"""Numbers that do not fit a finite float are refused when a share file or a pasted shader is
parsed, before anything is stored."""

import json
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from sb_fakes import PLUGIN, call  # noqa: E402
from shaderbruecke import library, record  # noqa: E402

HUGE = 10 ** 400


def share(**extra):
    data = {"format": library.SHARE_FORMAT, "name": "glow", "source": sb_fakes.SKY,
            "tuned_values": {"sunHeight": 0.5}, "licence": "MIT"}
    data.update(extra)
    return data


def versions(db) -> int:
    conn = sqlite3.connect(db)
    try:
        record.ensure_schema(conn)
        return conn.execute("SELECT COUNT(*) FROM shaders").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    return sb_fakes.fresh_profile(tmp_path, monkeypatch)


@pytest.mark.parametrize("value", [HUGE, -HUGE, float("inf"), float("nan")], ids=["huge", "-huge", "inf", "nan"])
def test_parse_share_refuses_a_tuned_value_that_is_not_a_finite_float(value):
    parsed, why = library.parse_share(share(tuned_values={"sunHeight": value}))
    assert parsed is None and "sunHeight" in why and "finite" in why


def test_parse_share_still_accepts_ordinary_numbers():
    parsed, why = library.parse_share(share(tuned_values={"sunHeight": 1, "other": 0.25}))
    assert why is None and parsed["tuned_values"] == {"sunHeight": 1, "other": 0.25}


def test_a_share_file_with_a_huge_number_is_refused_and_stores_nothing(profile):
    db, base = profile
    path = base / "glow.shaderbruecke.json"
    path.write_text(json.dumps(share(tuned_values={"sunHeight": HUGE})))
    answer = call(PLUGIN.shader_library, {"action": "import", "path": str(path)})
    assert answer["success"] is False and "finite" in json.dumps(answer)
    assert versions(db) == 0


def test_the_import_route_answers_a_refusal_not_an_exception(profile):
    db, base = profile
    conn = sqlite3.connect(db)
    try:
        answer = PLUGIN.import_shader(conn, base, {"share": share(tuned_values={"sunHeight": HUGE})})
    finally:
        conn.close()
    assert answer["ok"] is False
    assert versions(db) == 0


ISF = ('/*{"ISFVSN": "2", "INPUTS": [%s]}*/\n'
       "void main() { gl_FragColor = vec4(isf_FragNormCoord.x, 0.0, 0.0, 1.0); }\n")


@pytest.mark.parametrize("item", [
    '{"NAME": "count", "TYPE": "long", "DEFAULT": 1e400}',
    '{"NAME": "gain", "TYPE": "float", "MIN": 0, "MAX": 1e400, "DEFAULT": 0.5}',
    '{"NAME": "gain", "TYPE": "float", "MIN": -1e400, "MAX": 1, "DEFAULT": 0.5}',
    '{"NAME": "tint", "TYPE": "color", "DEFAULT": [1e400, 0, 0, 1]}',
    '{"NAME": "at", "TYPE": "point2D", "DEFAULT": [0, 1e999]}',
], ids=["long", "float-max", "float-min", "color", "point2D"])
def test_a_pasted_isf_shader_with_an_overflowing_number_is_refused(profile, item):
    db, _base = profile
    answer = call(PLUGIN.shader_library, {"action": "import", "source": ISF % item})
    assert answer["success"] is False and "not a finite number" in json.dumps(answer)
    assert versions(db) == 0


def test_clamping_a_stored_huge_integer_drops_it_instead_of_raising():
    doc = {"sliders": record.parse_sliders(sb_fakes.SKY), "tuned_values": {"sunHeight": HUGE}}
    assert record.tuned_values(doc) == {}
