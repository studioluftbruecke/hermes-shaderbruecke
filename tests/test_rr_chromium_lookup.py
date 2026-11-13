"""Chromium is found where a Mac installs it, not only through PATH or an environment variable."""

import importlib.util
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_renderer():
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer_lookup", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def executable(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_an_app_bundle_chrome_is_found_when_nothing_else_names_one(tmp_path, monkeypatch):
    renderer = load_renderer()
    chrome = executable(tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    monkeypatch.delenv("SHADERBRUECKE_CHROMIUM", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(renderer, "HERMES_CHROMIUM_MARKER", tmp_path / "no-marker")
    monkeypatch.setattr(renderer, "APP_CHROMIUM_PATHS", (str(tmp_path / "missing"), str(chrome)))
    assert renderer.find_chromium() == str(chrome)


def test_the_environment_override_still_wins(tmp_path, monkeypatch):
    renderer = load_renderer()
    mine = executable(tmp_path / "bin" / "my-chromium")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(mine))
    assert renderer.find_chromium() == str(mine)


def test_the_mac_locations_are_listed():
    renderer = load_renderer()
    assert "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" in renderer.APP_CHROMIUM_PATHS
    assert "/Applications/Chromium.app/Contents/MacOS/Chromium" in renderer.APP_CHROMIUM_PATHS
    assert all(os.path.isabs(path) for path in renderer.APP_CHROMIUM_PATHS)
