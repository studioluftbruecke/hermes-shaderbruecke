"""Shared-gateway robustness: at most two Chromium children per profile, timeouts that name their
phase, retention of old frames, failed renders outside the version folders, `list_recent` in SQL,
closed connections and settings read on every call.
"""

import json
import pathlib
import sqlite3
import stat
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import record as RECORD  # noqa: E402
from shaderbruecke import renderer as RENDERER  # noqa: E402
from shaderbruecke import settings as SETTINGS  # noqa: E402
from shaderbruecke.exporters import header as HEADER  # noqa: E402

OK_SOURCE = "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(1.0); }"


def fake_browser(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    script = tmp_path / "fake-chromium"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


# Chromium slots --------------------------------------------------------------------------------

def test_a_third_render_waits_for_one_of_two_slots(tmp_path, monkeypatch):
    log = tmp_path / "children.log"
    script = fake_browser(tmp_path, f"echo start $(date +%s.%N) >> '{log}'\nsleep 1\n"
                                    f"echo end $(date +%s.%N) >> '{log}'")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(script))
    results = []

    def one(index: int) -> None:
        with RENDERER.chromium_slots(tmp_path / "locks"):
            results.append(RENDERER.render(OK_SOURCE, tmp_path / f"out{index}", timeout_s=10.0))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    events = sorted((float(stamp), kind) for kind, stamp in
                    (line.split() for line in log.read_text(encoding="utf-8").splitlines()))
    running = peak = 0
    for _stamp, kind in events:
        running += 1 if kind == "start" else -1
        peak = max(peak, running)
    assert len(results) == 3 and peak == 2, events
    waits = sorted(result.slot_wait_ms or 0 for result in results)
    assert waits[-1] >= 700 and waits[0] < 500, waits


def test_a_render_that_cannot_get_a_slot_says_busy(tmp_path, monkeypatch):
    script = fake_browser(tmp_path, "sleep 5")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(script))
    monkeypatch.setattr(RENDERER, "SLOT_WAIT_S", 0.5)
    import fcntl
    import os

    locks = tmp_path / "locks"
    locks.mkdir()
    held = []
    for index in range(RENDERER.MAX_CHILDREN):
        fd = os.open(locks / f"chromium-{index}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.append(fd)
    try:
        with RENDERER.chromium_slots(locks):
            result = RENDERER.render_bundle(OK_SOURCE, tmp_path / "out", timeout_s=5.0)
    finally:
        for fd in held:
            os.close(fd)
    assert result.status == "busy"
    assert "2" in result.detail and "at once" in result.detail


# Deadlines -------------------------------------------------------------------------------------

def test_a_browser_that_never_answers_times_out_in_the_start_phase(tmp_path, monkeypatch):
    script = fake_browser(tmp_path, "sleep 30")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(script))
    started = time.monotonic()
    result = RENDERER.render_bundle(OK_SOURCE, tmp_path / "out", timeout_s=2.0)
    assert time.monotonic() - started < 15
    assert result.status == "timeout" and result.phase == "start"
    assert "loop" not in result.detail and "start" in result.detail


@pytest.mark.chromium
def test_a_slow_draw_times_out_in_the_frames_phase(tmp_path):
    slow = ("void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
            "  float acc = 0.0;\n"
            "  for (int i = 0; i < 200000000; i++) { acc += sin(float(i) * fragCoord.x * 1e-7); }\n"
            "  fragColor = vec4(vec3(fract(acc)), 1.0);\n}\n")
    result = RENDERER.render_bundle(slow, tmp_path / "out", timeout_s=12.0)
    assert result.status == "timeout", result
    assert result.phase == "frames", result
    assert "loop" in result.detail


# Pruning old versions --------------------------------------------------------------------------

def make_versions(base: pathlib.Path, shader_id: str, count: int) -> None:
    for version in range(1, count + 1):
        folder = RECORD.version_dir(base, shader_id, version)
        (folder / "exports" / "three").mkdir(parents=True)
        (folder / "exports" / "agent").mkdir(parents=True)
        for name in ("shader.frag", "result.json", "frame-t0.png", "frame-t1.png",
                     "contact-sheet.png", "browser.log"):
            (folder / name).write_text(name, encoding="utf-8")
        for name in ("SkyMaterial.js", "proof.json", "proof-reference.png", "proof-export.png"):
            (folder / "exports" / "three" / name).write_text(name, encoding="utf-8")
        for name in ("Sky.handoff.md", "reference-t1.png"):
            (folder / "exports" / "agent" / name).write_text(name, encoding="utf-8")


def test_retention_prunes_frames_of_all_but_the_newest_versions(tmp_path):
    base = tmp_path / "data"
    make_versions(base, "sky", 7)
    removed = RECORD.prune_versions(base, "sky", keep=5)
    assert removed > 0
    for version in (1, 2):
        folder = RECORD.version_dir(base, "sky", version)
        left = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
        assert left == ["exports/agent/Sky.handoff.md", "exports/agent/reference-t1.png",
                        "exports/three/SkyMaterial.js", "exports/three/proof.json",
                        "result.json", "shader.frag"], left
    for version in range(3, 8):
        assert (RECORD.version_dir(base, "sky", version) / "frame-t0.png").is_file()


def test_a_failed_render_leaves_no_version_folder(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    result = json.loads(PLUGIN.shader_render({"source": "void main() {}", "name": "broken"}))
    assert result["success"] is False and result["error"].startswith("lint_error")
    assert not (base / "shaders").exists() or not any((base / "shaders").rglob("*"))
    assert any((base / "scratch").rglob("shader.frag"))


def test_keep_versions_is_a_declared_setting():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "keep_versions:" in manifest and "type: int" in manifest
    assert SETTINGS.normalise({})["keep_versions"] == 5
    assert SETTINGS.normalise({"keep_versions": "0"})["keep_versions"] == 1
    assert SETTINGS.normalise({"keep_versions": 3, "credit_line": "false"}) == \
        {"allow_project_writes": False, "credit_line": False, "keep_versions": 3, "author_name": "", "author_url": "",
         "starter_prompt": True}


def test_author_settings_are_one_line_and_the_url_must_be_a_web_link():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "author_name:" in manifest and "author_url:" in manifest
    clean = SETTINGS.normalise({"author_name": "  Ada\n Example ", "author_url": "https://ada.example/x"})
    assert clean["author_name"] == "Ada Example" and clean["author_url"] == "https://ada.example/x"
    assert SETTINGS.normalise({"author_url": "javascript:alert(1)"})["author_url"] == ""
    assert SETTINGS.normalise({"author_name": 7})["author_name"] == ""


# Library queries -------------------------------------------------------------------------------

def test_list_recent_reads_only_the_latest_row_per_shader(tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    RECORD.ensure_schema(conn)
    conn.execute("INSERT INTO shaders VALUES ('a', 1, 'not json at all')")
    conn.commit()
    for shader_id, version, created in (("a", 2, "2026-11-08T02:00:00Z"),
                                        ("b", 1, "2026-11-08T03:00:00Z"),
                                        ("c", 1, "2026-11-08T01:00:00Z")):
        RECORD.save(conn, {"id": shader_id, "version": version, "name": shader_id,
                           "created_at": created, "sliders": []})
    rows = RECORD.list_recent(conn, 2)
    assert [(r["id"], r["version"]) for r in rows] == [("b", 1), ("a", 2)]


class TrackedConnection:
    """A sqlite connection that remembers whether the handler closed it."""

    def __init__(self, path):
        self.inner = sqlite3.connect(path)
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        self.closed = True
        self.inner.close()


def test_every_tool_call_closes_its_connection(tmp_path, monkeypatch):
    opened = []

    def storage():
        conn = TrackedConnection(tmp_path / "data.db")
        opened.append(conn)
        return conn, tmp_path / "data"

    monkeypatch.setattr(PLUGIN, "_storage", storage)
    PLUGIN.shader_library({"action": "list"})
    PLUGIN.shader_library({"action": "get", "id": "nope"})
    PLUGIN.shader_render({"source": "void main() {}"})
    PLUGIN.shader_export({"id": "nope", "target": "three"})
    assert opened and all(conn.closed for conn in opened)


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, handler, **kwargs):
        self.tools[name] = handler

    def register_skill(self, *args, **kwargs):
        pass

    def get_config(self, key, default=None):
        return default


def test_a_settings_change_applies_to_the_next_call_without_a_reload(tmp_path, monkeypatch):
    from shaderbruecke import exporting

    db = tmp_path / "data.db"
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), tmp_path / "data"))
    monkeypatch.setattr(exporting, "prove", lambda *a, **k: {
        "method": "fake", "verdict": "match", "why": "test", "checks": [], "diff": None,
        "frames": []})
    source = OK_SOURCE
    conn = sqlite3.connect(db)
    RECORD.save(conn, {"id": "sky", "version": 1, "name": "sky", "source": source, "sliders": [],
                       "provenance": {}, "files": {}})
    conn.close()
    ctx = FakeContext()
    PLUGIN.register(ctx)
    current = {"allow_project_writes": False, "credit_line": True, "keep_versions": 5}
    monkeypatch.setattr(SETTINGS, "read_settings", lambda: dict(current))

    def material_text():
        result = json.loads(ctx.tools["shader_export"]({"id": "sky", "target": "three"}))
        path = next(f["path"] for f in result["files"] if f["path"].endswith("Material.js"))
        return pathlib.Path(path).read_text(encoding="utf-8")

    assert HEADER.CREDIT in material_text()
    current["credit_line"] = False
    assert HEADER.CREDIT not in material_text()
