"""A render budget the agent sees in the tool result.

For "make it and show it" one model rendered 11 versions of one shader in a 21-minute
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
    second = json.loads(PLUGIN.shader_render({"source": GOOD + "// v2\n", "name": "sky", "id": first["id"]}))
    third = json.loads(PLUGIN.shader_render({"source": GOOD + "// v3\n", "name": "sky", "id": first["id"]}))
    assert "Stop here" not in first["next"] and "Stop here" not in second["next"], (first["next"], second["next"])
    assert third["version"] == 3 and "Stop here" in third["next"], third["next"]
    assert third["versions_last_15_min"] == 3
    assert third["attempts_last_15_min"] == 3


def test_a_fourth_version_within_the_window_is_refused_before_the_browser(tmp_path, monkeypatch):
    # A live turn showed a model ignore "Stop here" at v3, v4 and v5, so the
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
    for version in (2, 3):
        PLUGIN.shader_render({"source": GOOD + f"// v{version}\n", "name": "sky", "id": first["id"]})
    fourth = json.loads(PLUGIN.shader_render({"source": GOOD + "// v4\n", "name": "sky", "id": first["id"],
                                              "keep_iterating": True}))
    assert fourth["success"] is True and fourth["version"] == 4, fourth


def test_compile_failures_consume_the_budget_without_losing_the_failed_attempt(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    browser_calls = []

    def compile_error(source, out_dir, **kwargs):
        browser_calls.append(out_dir)
        diagnostic = PLUGIN.renderer.Diagnostic("error", "shader", 1, "undeclared identifier")
        return PLUGIN.renderer.BundleResult("compile_error", compile_log="ERROR: 1:1: undeclared identifier",
                                            errors=[diagnostic])

    monkeypatch.setattr(PLUGIN.renderer, "render_bundle", compile_error)
    first = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "broken"}))
    shader_id = first["id"]
    for _ in range(2):
        result = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "broken", "id": shader_id}))
        assert result["error"].startswith("compile error"), result
    assert len(browser_calls) == 3

    scratch = base / "scratch" / shader_id
    attempts = list(scratch.iterdir())
    assert len(attempts) == 1
    assert json.loads((attempts[0] / "result.json").read_text())["status"] == "compile_error"

    refused = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "broken", "id": shader_id}))
    assert refused["success"] is False and refused["error"].startswith("render_budget"), refused
    assert refused["attempts_last_15_min"] == 3
    assert len(browser_calls) == 3
    assert (attempts[0] / "result.json").is_file()

    override = json.loads(PLUGIN.shader_render({"source": GOOD, "name": "broken", "id": shader_id,
                                                "keep_iterating": True}))
    assert override["error"].startswith("compile error"), override
    assert len(browser_calls) == 4


@pytest.mark.chromium
def test_real_chromium_stops_after_three_compile_failures_and_honours_the_override(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    starts = []
    original_enter = PLUGIN.renderer._Child.__enter__

    def counting_enter(child):
        starts.append(child.out_dir)
        return original_enter(child)

    monkeypatch.setattr(PLUGIN.renderer._Child, "__enter__", counting_enter)
    broken = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) "
              "{ fragColor = vec4(sb_missing_identifier); }")
    first = json.loads(PLUGIN.shader_render({"source": broken, "name": "broken"}))
    shader_id = first["id"]
    for _ in range(2):
        result = json.loads(PLUGIN.shader_render({"source": broken, "name": "broken", "id": shader_id}))
        assert result["error"].startswith("compile error"), result
    assert len(starts) == 3

    refused = json.loads(PLUGIN.shader_render({"source": broken, "name": "broken", "id": shader_id}))
    assert refused["success"] is False and refused["error"].startswith("render_budget"), refused
    assert len(starts) == 3

    override = json.loads(PLUGIN.shader_render({"source": broken, "name": "broken", "id": shader_id,
                                                "keep_iterating": True}))
    assert override["error"].startswith("compile error"), override
    assert len(starts) == 4


def test_attempts_survive_reconnects_and_a_success_is_not_counted_twice(tmp_path):
    db = tmp_path / "data.db"
    now = time.time()
    first = sqlite3.connect(db)
    RECORD.start_attempt(first, "sky", "attempt-1", now=now)
    first.close()

    conn = sqlite3.connect(db)
    assert RECORD.recent_attempts(conn, "sky", within_s=900, now=now) == 1
    RECORD.finish_attempt(conn, "sky", "attempt-1", 1)
    RECORD.save(conn, {"id": "sky", "version": 1, "created_at": RECORD.now_iso()})
    assert RECORD.recent_attempts(conn, "sky", within_s=900, now=now) == 1

    RECORD.start_attempt(conn, "sky", "expired", now=now - 901)
    assert RECORD.recent_attempts(conn, "sky", within_s=900, now=now) == 1
    conn.close()
