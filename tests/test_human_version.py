"""The Lab's Save: a human version through the same lint and render as the agent's, the
stale-base and busy refusals, an inline compile error that makes no version, and the one line
the agent's next turn gets (`pre_llm_call`). The render is faked; no browser, no Hermes."""

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke  # noqa: E402
from shaderbruecke import record, renderer  # noqa: E402
from shaderbruecke.dashboard import library_api  # noqa: E402

SOURCE = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }\n"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    record.ensure_schema(conn)
    record.save(conn, {"id": "a-sky", "version": 1, "name": "sky", "source": SOURCE, "sliders": [],
                       "tuned_values": {}, "provenance": {"origin": "agent", "licence": None, "parent": None},
                       "checks": {"status": "ok", "flash": {"verdict": "none"}}, "author_kind": "agent",
                       "created_at": "2026-11-09T00:00:00Z", "files": {}})
    record.save_meta(conn, record.new_meta("a-sky", "Dusk \u2014 sky"))
    calls = []

    def fake_render_version(conn_, base_, **kwargs):
        calls.append(kwargs)
        if "BROKEN" in kwargs["source"]:
            return {"success": False, "error": "compile error: 1 diagnostic(s)",
                    "errors": [{"line": 1, "where": "shader", "message": "'BROKEN' : undeclared identifier"}]}
        version = record.next_version(conn_, kwargs["requested_id"])
        record.save(conn_, {**record.latest(conn_, kwargs["requested_id"]), "version": version,
                            "source": kwargs["source"], "author_kind": kwargs["author_kind"]})
        return {"success": True, "id": kwargs["requested_id"], "version": version}

    monkeypatch.setattr(shaderbruecke, "render_version", fake_render_version)
    monkeypatch.setattr(shaderbruecke, "_storage", lambda: (sqlite3.connect(path), tmp_path))
    monkeypatch.setattr(shaderbruecke, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "free_slots", lambda directory: 2)
    yield conn, tmp_path, calls
    conn.close()


def test_a_save_becomes_a_human_version_and_the_next_turn_hears_of_it_once(lib):
    conn, base, calls = lib
    answer = library_api.save_version(conn, base, "a-sky", {"source": SOURCE.replace("1.0", "0.5"), "base_version": 1})
    assert answer["ok"] and answer["version"]["version"] == 2 and answer["version"]["authorKind"] == "human"
    assert answer["row"]["authorKind"] == "human"
    assert calls[0]["author_kind"] == "human" and calls[0]["requested_id"] == "a-sky"
    context = shaderbruecke.on_pre_llm_call(session_id="s1", user_message="hi")
    assert context == {"context": 'Shaderbrücke: the user edited "Dusk - sky" (now v2, id a-sky) in the Lab since '
                                  "your last turn; read it with shader_library get before you change it, and "
                                  "build on their edit."}
    assert shaderbruecke.on_pre_llm_call(session_id="s1", user_message="again") is None


def test_the_next_turn_never_names_a_deleted_shader(lib):
    conn, base, _calls = lib
    library_api.save_version(conn, base, "a-sky", {"source": SOURCE.replace("1.0", "0.5"), "base_version": 1})
    record.delete(conn, base, "a-sky")
    assert shaderbruecke.on_pre_llm_call(session_id="s1", user_message="hi") is None


def test_a_stale_base_is_refused_with_the_latest_version(lib):
    conn, base, calls = lib
    library_api.save_version(conn, base, "a-sky", {"source": SOURCE, "base_version": 1})
    stale = library_api.save_version(conn, base, "a-sky", {"source": SOURCE, "base_version": 1})
    assert stale["status"] == 409 and stale["error"] == "stale_base" and stale["latest"] == 2
    assert "a newer version exists" in stale["message"] and len(calls) == 1


def test_busy_renderers_answer_at_once(lib, monkeypatch):
    conn, base, calls = lib
    monkeypatch.setattr(renderer, "free_slots", lambda directory: 0)
    busy = library_api.save_version(conn, base, "a-sky", {"source": SOURCE, "base_version": 1})
    assert busy["status"] == 409 and busy["error"] == "busy" and calls == []


def test_a_compile_error_comes_back_inline_and_makes_no_version(lib):
    conn, base, _calls = lib
    failed = library_api.save_version(conn, base, "a-sky", {"source": "BROKEN", "base_version": 1})
    assert failed == {"ok": False, "error": "compile_error", "message": "compile error: 1 diagnostic(s)",
                      "diagnostics": [{"line": 1, "severity": "error", "message": "'BROKEN' : undeclared identifier"}]}
    assert record.latest(conn, "a-sky")["version"] == 1
    assert shaderbruecke.on_pre_llm_call() is None


@pytest.mark.parametrize("body, code", [({"base_version": 1}, 400), ({"source": SOURCE}, 400),
                                        ({"source": SOURCE, "base_version": True}, 400)])
def test_bad_saves_are_refused(lib, body, code):
    conn, base, _calls = lib
    assert library_api.save_version(conn, base, "a-sky", body)["status"] == code
    assert library_api.save_version(conn, base, "nope", {"source": SOURCE, "base_version": 1})["status"] == 404
