"""A profile whose temp folder has a long path still renders.

Hermes points TMPDIR at `<profile home>/cache/scratch`. Chromium puts its singleton socket under
TMPDIR, and a Unix socket path over ~107 bytes makes the browser exit at start ("browser closed
the DevTools pipe"), so every render of such a profile failed.
"""

import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = "void mainImage(out vec4 c, in vec2 p) { c = vec4(p.x / iResolution.x, 0.4, 0.6, 1.0); }"


def load_renderer():
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer_tmp", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def long_tmpdir(tmp_path: pathlib.Path) -> pathlib.Path:
    deep = tmp_path / ("profile-home-" + "x" * 40) / "cache" / ("scratch-" + "y" * 30)
    deep.mkdir(parents=True)
    assert len(str(deep)) > 90
    return deep


def test_the_child_gets_a_short_temp_dir(tmp_path, monkeypatch):
    renderer = load_renderer()
    monkeypatch.setenv("TMPDIR", str(long_tmpdir(tmp_path)))
    base = renderer.short_temp_base()
    assert len(base) <= renderer.MAX_TEMP_BASE
    assert os.path.isdir(base) and os.access(base, os.W_OK)
    assert renderer.child_env(base + "/profile")["TMPDIR"] == base


@pytest.mark.chromium
def test_a_long_tmpdir_still_renders(tmp_path, monkeypatch):
    renderer = load_renderer()
    monkeypatch.setenv("TMPDIR", str(long_tmpdir(tmp_path)))
    result = renderer.render(SOURCE, tmp_path / "out", timeout_s=30.0)
    assert result.status == "ok", result.detail
