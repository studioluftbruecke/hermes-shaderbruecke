"""F28: a render budget the agent sees in the tool result (full test, 2026-11-08).

For "make it and show it" DeepSeek v4.1 Flash rendered 11 versions of one shader in a 21-minute
turn; the skill said "fix and render again until the checks and your eyes agree" with no stop.
The skill now sets a budget of three renders per request, and because a model can skip a skill,
`shader_render` itself says so from the third version made within 15 minutes.
"""

import json
import pathlib
import sqlite3
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402

GOOD = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(fragCoord.x / iResolution.x, 0.3, 0.5, 1.0); }"


def test_recent_versions_counts_only_the_window(tmp_path):
    conn = sqlite3.connect(tmp_path / "d.db")
    for version, stamp in ((1, "2026-11-08T20:00:00Z"), (2, "2026-11-08T21:50:00Z"), (3, "2026-11-08T21:59:00Z")):
        RECORD.save(conn, {"id": "sky", "version": version, "created_at": stamp})
    now = 1794175200.0  # 2026-11-08T22:00:00Z
    assert RECORD.recent_versions(conn, "sky", within_s=900, now=now) == 2


def test_the_skill_sets_a_budget():
    skill = (ROOT / "skills" / "shader-craft" / "SKILL.md").read_text(encoding="utf-8")
    assert "three `shader_render` calls" in skill


@pytest.mark.chromium
def test_the_third_version_in_a_row_tells_the_agent_to_stop(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    first = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky"}))
    second = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky", "id": first["id"]}))
    third = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky", "id": first["id"]}))
    assert "Stop here" not in first["next"] and "Stop here" not in second["next"], (first["next"], second["next"])
    assert third["version"] == 3 and "Stop here" in third["next"], third["next"]
    assert third["versions_last_15_min"] == 3


def test_a_fourth_version_within_the_window_is_refused_before_the_browser(tmp_path, monkeypatch):
    # The live box (runs/f28-budget) showed DeepSeek ignore "Stop here" at v3, v4 and v5, so the
    # budget is enforced: no browser starts, and the refusal hands back the latest version to show.
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    conn = sqlite3.connect(db)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for version in (1, 2, 3):
        RECORD.save(conn, {"id": "sky", "version": version, "name": "sky", "created_at": stamp,
                           "files": {"frag": f"/data/shaders/sky/v{version}/shader.frag"}})
    conn.close()

    def no_browser(*args, **kwargs):
        raise AssertionError("the budget must stop this render before a browser starts")

    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", no_browser)
    refused = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky", "id": "sky"}))
    assert refused["success"] is False and refused["error"].startswith("render_budget"), refused
    assert refused["latest_version"] == 3 and refused["media"] == "MEDIA:/data/shaders/sky/v3/shader.frag"
    assert "keep_iterating" in refused["next"]


@pytest.mark.chromium
def test_keep_iterating_lets_the_user_ask_for_more(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    first = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky"}))
    for _ in range(2):
        PLUGIN.shader_render({"source": GOOD, "name": "sky", "id": first["id"]})
    fourth = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "sky", "id": first["id"],
                                              "keep_iterating": True}))
    assert fourth["success"] is True and fourth["version"] == 4, fourth
