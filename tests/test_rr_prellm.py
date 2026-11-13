"""The pre_llm_call hook runs before every LLM call in the profile, so with no Lab save pending it
answers from one file check and never opens the database."""

import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke  # noqa: E402
from shaderbruecke import record  # noqa: E402


def test_no_pending_save_means_no_database_work(tmp_path, monkeypatch):
    def no_db():
        raise AssertionError("the hook opened the database with nothing pending")

    monkeypatch.setattr(shaderbruecke, "_storage", no_db)
    monkeypatch.setattr(shaderbruecke, "_data_dir", lambda: tmp_path)
    assert shaderbruecke.on_pre_llm_call(session_id="s1", user_message="hi") is None


def test_a_pending_save_is_delivered_once(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db)
    record.ensure_schema(conn)
    record.save_meta(conn, record.new_meta("a-sky", "sky"))
    record.add_human_notice(conn, tmp_path, "a-sky", 2)
    conn.close()
    monkeypatch.setattr(shaderbruecke, "_storage", lambda: (sqlite3.connect(db), tmp_path))
    monkeypatch.setattr(shaderbruecke, "_data_dir", lambda: tmp_path)
    assert "now v2, id a-sky" in shaderbruecke.on_pre_llm_call()["context"]
    monkeypatch.setattr(shaderbruecke, "_storage", lambda: (_ for _ in ()).throw(AssertionError("opened")))
    assert shaderbruecke.on_pre_llm_call() is None


def test_a_lab_save_left_pending_by_an_older_version_is_told_once(tmp_path, monkeypatch):
    # Older versions stored a Lab save only as a database row, without the marker file.
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db)
    record.ensure_schema(conn)
    record.save_meta(conn, record.new_meta("a-sky", "sky"))
    conn.execute("INSERT INTO human_notices (id, version, created_at, delivered_at) VALUES (?, ?, ?, NULL)",
                 ("a-sky", 3, record.now_iso()))
    conn.commit()
    conn.close()
    opened = []

    def storage():
        opened.append(1)
        return sqlite3.connect(db), tmp_path

    monkeypatch.setattr(shaderbruecke, "_storage", storage)
    monkeypatch.setattr(shaderbruecke, "_data_dir", lambda: tmp_path)
    assert "now v3, id a-sky" in shaderbruecke.on_pre_llm_call()["context"]
    # Once read, the next model calls go back to the one file check.
    assert shaderbruecke.on_pre_llm_call() is None
    assert shaderbruecke.on_pre_llm_call() is None
    assert len(opened) == 1


def test_a_library_with_nothing_pending_is_read_once_then_never_again(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db)
    record.ensure_schema(conn)
    conn.close()
    opened = []

    def storage():
        opened.append(1)
        return sqlite3.connect(db), tmp_path

    monkeypatch.setattr(shaderbruecke, "_storage", storage)
    monkeypatch.setattr(shaderbruecke, "_data_dir", lambda: tmp_path)
    for _ in range(5):
        assert shaderbruecke.on_pre_llm_call() is None
    assert len(opened) <= 1
