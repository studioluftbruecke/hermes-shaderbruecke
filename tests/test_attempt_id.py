"""The `id` a failed render returns can be continued.

A failed render (lint or compile error) answers with an `id`, and agents pass it straight back as
`shader_render(id=...)`. That call was refused ("no shader with id ..."), because failed attempts
never become a version: a model lost a call after each of its two compile errors. An id the plugin
minted for a failed attempt now continues as that shader's version 1, with the lineage the failed
attempt had. An id nobody minted is still refused.
"""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402

GOOD = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(0.2, 0.4, 0.6, 1.0); }"
LINT_BROKEN = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { gl_FragColor = vec4(1.0); }"


@pytest.fixture
def render(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    return lambda **args: json.loads(PLUGIN.shader_render(args))


def test_an_unknown_id_is_still_refused(render):
    result = render(source=GOOD, name="x", id="20261108-000000-000000-never-minted")
    assert result["success"] is False and "no shader with id" in result["error"], result


def test_a_failed_render_says_its_id_continues(render):
    failed = render(source=LINT_BROKEN, name="broken")
    assert failed["success"] is False and failed["id"], failed
    assert "same id" in failed["next"], failed["next"]


@pytest.mark.chromium
def test_the_id_of_a_failed_first_render_continues_as_version_1(render):
    failed = render(source=LINT_BROKEN, name="broken")
    fixed = render(source=GOOD, name="broken", id=failed["id"])
    assert fixed["success"] is True, fixed
    assert fixed["id"] == failed["id"] and fixed["version"] == 1, fixed
    again = render(source=GOOD + "// v2\n", name="broken", id=failed["id"])
    assert again["version"] == 2, again


@pytest.mark.chromium
def test_a_failed_fork_keeps_its_parent_when_continued(render):
    parent = render(source=GOOD, name="ocean")
    failed = render(source=LINT_BROKEN, name="foam", parent_id=parent["id"])
    fixed = render(source=GOOD, name="foam", id=failed["id"])
    assert fixed["success"] is True and fixed["id"] == failed["id"], fixed
    assert fixed.get("parent_id") == parent["id"], fixed
