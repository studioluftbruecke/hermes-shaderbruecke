"""The Chromium child never sees the host's environment (the gateway holds every API key)."""

import importlib.util
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SENTINEL = "SHADERBRUECKE_TEST_SECRET"


def load_renderer():
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_browser(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """An executable that records the environment it was started with, then exits."""
    dump = tmp_path / "child-env.txt"
    script = tmp_path / "fake-chromium"
    script.write_text(f"#!/bin/sh\nenv > '{dump}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, dump


def test_no_host_variable_reaches_the_browser(tmp_path, monkeypatch):
    renderer = load_renderer()
    script, dump = fake_browser(tmp_path)
    monkeypatch.setenv(SENTINEL, "sk-should-never-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-also-never")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(script))

    result = renderer.render("void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }",
                             tmp_path / "out", timeout_s=5.0)

    assert result.status == "browser_error"
    child = dict(line.split("=", 1) for line in dump.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert SENTINEL not in child
    assert "OPENAI_API_KEY" not in child
    assert "SHADERBRUECKE_CHROMIUM" not in child
    assert pathlib.Path(child["HOME"]).name.startswith("shaderbruecke-chrome-")
    assert not os.path.exists(child["HOME"]), "the throwaway profile is removed after the render"


def test_child_env_keeps_only_lookup_locale_and_tmp(monkeypatch):
    renderer = load_renderer()
    monkeypatch.setenv(SENTINEL, "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = renderer.child_env("/tmp/profile")
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "HOME"}
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/profile"
