"""Tool arguments of the wrong type or out of range are refused with a sentence, never raised.

Also: a render time of infinity is refused before anything is stored, because a record carrying
`Infinity` is not JSON and the shader would drop off the library page and `list`.
"""

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from sb_fakes import PLUGIN, call  # noqa: E402


@pytest.fixture
def shader(tmp_path, monkeypatch):
    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    return db, base, sb_fakes.new_shader(db, base)


@pytest.mark.parametrize("args, word", [
    ({"action": "get", "version": float("nan")}, "version"),
    ({"action": "get", "version": float("inf")}, "version"),
    ({"action": "play", "version": 10 ** 400}, "version"),
    ({"action": "fork", "version": float("-inf")}, "version"),
    ({"action": "share", "version": float("nan")}, "version"),
    ({"action": "list", "limit": float("nan")}, "limit"),
    ({"action": "fork", "name": ["x"]}, "name"),
    ({"action": "fork", "name": {"a": 1}}, "name"),
], ids=["get-nan", "get-inf", "play-huge", "fork-version", "share-nan", "list-limit", "fork-name-list", "fork-name-dict"])
def test_shader_library_refuses_bad_arguments(shader, args, word):
    _db, _base, shader_id = shader
    answer = call(PLUGIN.shader_library, {"id": shader_id, **args})
    assert answer["success"] is False and word in answer["error"]


@pytest.mark.parametrize("version", [float("inf"), float("nan"), "two"])
def test_shader_export_refuses_a_bad_version(shader, version):
    _db, _base, shader_id = shader
    answer = call(PLUGIN.shader_export, {"id": shader_id, "target": "three", "version": version})
    assert answer["success"] is False and "version" in answer["error"]


def test_whole_numbers_still_work(shader):
    _db, _base, shader_id = shader
    assert call(PLUGIN.shader_library, {"action": "get", "id": shader_id, "version": 1})["success"] is True
    assert call(PLUGIN.shader_library, {"action": "get", "id": shader_id, "version": 1.0})["success"] is True
    assert call(PLUGIN.shader_library, {"action": "get", "id": shader_id, "version": "1"})["success"] is True
    assert call(PLUGIN.shader_library, {"action": "list", "limit": 5})["count"] == 1


@pytest.mark.parametrize("time", ["inf", float("inf"), float("nan"), "-inf", "1e400",
                                  pytest.param(10 ** 400, id="400-digit"),
                                  pytest.param(-(10 ** 400), id="minus-400-digit")])
def test_a_render_time_that_is_not_finite_is_refused_and_stores_nothing(shader, time):
    db, _base, shader_id = shader
    answer = call(PLUGIN.shader_render, {"source": sb_fakes.SKY + "// v2\n", "id": shader_id, "time": time,
                                         "keep_iterating": True})
    assert answer["success"] is False and "time" in answer["error"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM shaders WHERE NOT json_valid(record)").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(version) FROM shaders").fetchone()[0] == 1


@pytest.mark.parametrize("time", [2, 0.5, "1.5", None])
def test_an_ordinary_render_time_still_renders(shader, time):
    _db, _base, shader_id = shader
    args = {"source": sb_fakes.SKY + f"// {time!r}\n", "id": shader_id, "keep_iterating": True}
    if time is not None:
        args["time"] = time
    assert call(PLUGIN.shader_render, args)["success"] is True
