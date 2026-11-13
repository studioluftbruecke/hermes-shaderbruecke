"""Playing a stored version in the chat (`shader_library play`) and a render that would only repeat
the latest version.

Desktop draws the live card for a `MEDIA:` line only when the tool call that first named the path
belongs to a tool the card is registered for. `play` hands the card what a render result carries
without rendering or writing a version; `get` and `fork` stop naming the `.frag`, so a `get` before
a `play` in the same turn cannot become the path's producer. A `shader_render` whose source is
the latest version's returns that version instead of writing a duplicate.
"""

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke  # noqa: E402
from shaderbruecke import record, references, renderer  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402

SOURCE = ("// @slider glow 0.0..2.0 = 0.8 step 0.01 \"Glow\"\n"
          "// @slider speed 0.0..3.0 = 1.0\n"
          "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(glow * speed); }\n")
OK_CHECKS = {"status": "ok", "flat": False, "flash": {"verdict": "none"}, "motion": {"verdict": "moving"}}


def _version(base, shader_id, version, *, source=SOURCE, checks=None, tuned=None, frag=True):
    folder = record.version_dir(base, shader_id, version)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "shader.frag"
    if frag:
        path.write_text(source, encoding="utf-8")
    return {"id": shader_id, "version": version, "name": "ember ridges", "source": source,
            "sliders": record.parse_sliders(source), "tuned_values": dict(tuned or {}), "channels": [],
            "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
            "checks": dict(checks or OK_CHECKS), "edited_by": "agent", "author_kind": "agent",
            "created_at": f"2026-11-10T23:3{version}:00Z",
            "files": {"frag": str(path), "contact_sheet": str(folder / "contact-sheet.png"),
                      "snapshot": None, "thumb": None}}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    path = tmp_path / "data.db"
    base = tmp_path / "data"
    conn = sqlite3.connect(path)
    record.ensure_schema(conn)
    record.save(conn, _version(base, "ember", 1, tuned={"glow": 1.5}))
    record.save(conn, _version(base, "ember", 2, source=SOURCE.replace("0.8", "0.9")))
    record.save_meta(conn, record.new_meta("ember", "Ember ridges"))
    monkeypatch.setattr(shaderbruecke, "_storage", lambda: (sqlite3.connect(path), base))
    yield conn, base
    conn.close()


def call(**args):
    return json.loads(shaderbruecke.shader_library(args))


def frag(base, shader_id, version):
    return str(record.version_dir(base, shader_id, version) / "shader.frag")


# play ---------------------------------------------------------------------------------------------

def test_play_hands_the_card_what_a_render_result_carries(lib):
    conn, base = lib
    played = call(action="play", id="ember")
    assert played["success"] is True
    assert (played["id"], played["version"], played["name"]) == ("ember", 2, "Ember ridges")
    assert [s["name"] for s in played["sliders"]] == ["glow", "speed"]
    assert played["sliders"][0]["default"] == 0.9
    assert played["tuned_values"] == {}
    assert played["media"] == f"MEDIA:{frag(base, 'ember', 2)}"
    assert played["checks"]["status"] == "ok" and played["checks"]["flash"]["verdict"] == "none"
    assert "media" in played["next"]


def test_play_an_older_version_with_its_own_tuned_values(lib):
    conn, base = lib
    played = call(action="play", id="ember", version=1)
    assert played["version"] == 1
    assert played["media"] == f"MEDIA:{frag(base, 'ember', 1)}"
    assert played["tuned_values"] == {"glow": 1.5}
    assert played["sliders"][0]["default"] == 0.8


def test_play_writes_no_version(lib):
    conn, base = lib
    before = len(record.history(conn, "ember"))
    call(action="play", id="ember")
    call(action="play", id="ember", version=1)
    assert len(record.history(conn, "ember")) == before == 2
    assert sorted(p.name for p in (base / "shaders" / "ember").iterdir()) == ["v1", "v2"]


@pytest.mark.parametrize("checks, words", [
    ({**OK_CHECKS, "flat": True}, "flat"),
    ({**OK_CHECKS, "flash": {"verdict": "block"}}, "flash"),
    ({**OK_CHECKS, "flash": {"verdict": "warn"}}, "flash"),
    ({**OK_CHECKS, "status": "compile_error"}, "check"),
])
def test_a_version_that_may_not_autoplay_gets_no_media_line_and_the_reason(lib, checks, words):
    conn, base = lib
    record.save(conn, _version(base, "ember", 3, checks=checks))
    played = call(action="play", id="ember")
    assert played["success"] is True and played["version"] == 3
    assert "media" not in played
    assert words in played["warning"], played["warning"]
    assert "MEDIA:" not in json.dumps(played)


def test_a_version_whose_frag_is_gone_gets_no_media_line(lib):
    conn, base = lib
    record.save(conn, _version(base, "ember", 3, frag=False))
    played = call(action="play", id="ember")
    assert "media" not in played and "shader.frag" in played["warning"]


def test_play_an_unknown_id_or_version_is_an_error(lib):
    assert call(action="play", id="nope") == {"success": False, "error": "no shader nope"}
    missing = call(action="play", id="ember", version=7)
    assert missing["success"] is False and missing["error"] == "no shader ember version 7"
    assert call(action="play")["error"] == "id is required for play"


def test_get_and_fork_no_longer_name_the_frag_path(lib, monkeypatch):
    # Desktop credits a MEDIA line to the EARLIEST tool result naming the path. A get that names
    # it would own a later play's line, and the card would never draw.
    conn, base = lib
    got = call(action="get", id="ember", version=1)
    assert got["success"] is True and got["shader"]["source"] == SOURCE
    assert frag(base, "ember", 1) not in json.dumps(got)
    assert "shader.frag" not in json.dumps(got)
    forked = call(action="fork", id="ember")
    assert forked["success"] is True
    assert "shader.frag" not in json.dumps(forked)


def test_the_reference_says_how_to_play_it(lib):
    conn, base = lib
    text = references.expand_from(conn, "ember@v1")
    assert "Play it in the chat: shader_library play with id ember and version 1" in text


def test_the_tool_texts_name_play():
    library = shaderbruecke.SHADER_LIBRARY
    assert "play" in shaderbruecke.LIBRARY_ACTIONS
    assert "`play`" in library["description"]
    assert "play" in library["parameters"]["properties"]["version"]["description"]
    assert "shader_library play" in shaderbruecke.SHADER_RENDER["description"]


def test_the_skill_says_where_a_card_comes_from():
    skill = (ROOT / "skills" / "shader-craft" / "SKILL.md").read_text(encoding="utf-8")
    assert "shader_library play" in skill
    assert "Never copy a path from `get`" in skill


# import -----------------------------------------------------------------------------------------

def test_an_import_result_carries_name_sliders_and_tuned_values(lib, monkeypatch):
    conn, base = lib

    def fake_render_version(conn_, base_, **kwargs):
        doc = _version(base_, "imported", 1, source=kwargs["source"])
        record.save(conn_, doc)
        record.save_meta(conn_, record.new_meta("imported", kwargs["name"]))
        return {"success": True, "id": "imported", "version": 1}

    monkeypatch.setattr(shaderbruecke, "render_version", fake_render_version)
    imported = call(action="import", source=SOURCE, name="Pasted glow", licence="MIT")
    assert imported["success"] is True, imported
    assert (imported["id"], imported["version"], imported["name"]) == ("imported", 1, "Pasted glow")
    assert [s["name"] for s in imported["sliders"]] == ["glow", "speed"]
    assert imported["tuned_values"] == {}
    assert imported["media"] == f"MEDIA:{frag(base, 'imported', 1)}"


# tuning saves on the version the card shows -----------------------------------------------------

def test_tuning_saves_on_the_named_version_and_leaves_the_latest_alone(lib):
    conn, base = lib
    saved = library_api.set_values(conn, base, "ember", {"glow": 1.2}, version=1)
    assert saved == {"id": "ember", "version": 1, "tuned_values": {"glow": 1.2}}
    assert record.get_version(conn, "ember", 1)["tuned_values"] == {"glow": 1.2}
    assert record.latest(conn, "ember")["tuned_values"] == {}
    reset = library_api.reset_values(conn, base, "ember", version=1)
    assert reset["version"] == 1 and record.get_version(conn, "ember", 1)["tuned_values"] == {}
    assert library_api.set_values(conn, base, "ember", {"glow": 1.0}, version=9)["status"] == 404
    # Without a version the latest takes it, as the Shaders page has always done.
    assert library_api.set_values(conn, base, "ember", {"glow": 1.1})["version"] == 2


def test_a_thumb_is_refreshed_only_when_the_latest_version_was_tuned(lib, monkeypatch):
    conn, base = lib
    started = []
    monkeypatch.setattr(library_api.thumbs, "start_refresh",
                        lambda conn_, base_, factory, shader_id: started.append(shader_id) or {"job": 1})
    assert library_api.refresh_thumb(conn, base, None, "ember", version=1) == {}
    assert library_api.refresh_thumb(conn, base, None, "ember", version=2) == {"job": 1}
    assert library_api.refresh_thumb(conn, base, None, "ember") == {"job": 1}
    assert started == ["ember", "ember"]


# a render that repeats the latest version -------------------------------------------------------

def test_rendering_the_latest_source_again_writes_no_version(lib, monkeypatch):
    conn, base = lib

    def no_browser(*args, **kwargs):
        raise AssertionError("an unchanged source must not start a browser")

    monkeypatch.setattr(renderer, "render_bundle", no_browser)
    latest_source = record.latest(conn, "ember")["source"]
    again = json.loads(shaderbruecke.shader_render({"source": latest_source, "name": "ember ridges",
                                                    "id": "ember"}))
    assert again["success"] is True and again["unchanged"] is True
    assert again["version"] == 2 and again["id"] == "ember"
    assert again["media"] == f"MEDIA:{frag(base, 'ember', 2)}"
    assert "no new version" in again["next"]
    assert len(record.history(conn, "ember")) == 2


def test_a_changed_source_or_a_fork_still_renders(lib, monkeypatch):
    conn, base = lib
    reached = []

    def stop_at_browser(source, *args, **kwargs):
        reached.append(source)
        raise RuntimeError("browser reached")

    monkeypatch.setattr(renderer, "render_bundle", stop_at_browser)
    monkeypatch.setattr(renderer, "free_slots", lambda directory: 2)
    latest_source = record.latest(conn, "ember")["source"]
    with pytest.raises(RuntimeError):
        shaderbruecke.shader_render({"source": latest_source + "// changed\n", "id": "ember"})
    with pytest.raises(RuntimeError):
        shaderbruecke.shader_render({"source": latest_source, "parent_id": "ember"})
    assert len(reached) == 2
