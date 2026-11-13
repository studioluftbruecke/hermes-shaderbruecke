"""Chromium starts with its sandbox, and drops it only on a host where the sandbox cannot start.

The fake browser exits at once without `--no-sandbox` when told the host has no usable sandbox
(as a container that refuses user namespaces does); otherwise it answers on the DevTools pipe.
"""

import importlib.util
import pathlib
import sys
import time

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import sb_fake_chromium as fake  # noqa: E402

PAGE_TEXT = "<!doctype html><title>probe</title>"


@pytest.fixture
def renderer(tmp_path):
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer_sandbox", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(renderer, tmp_path, name):
    page = tmp_path / "site" / "probe.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(PAGE_TEXT)
    return renderer.run_page(page, "1", timeout_s=10, out_dir=tmp_path / name)


def sandboxed(argv) -> bool:
    return "--no-sandbox" not in argv


def test_a_host_with_a_working_sandbox_keeps_it(renderer, tmp_path, monkeypatch):
    browser = fake.install(tmp_path / "bin", sandbox="ok")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(browser))
    assert renderer.sandbox_state() == "unknown"
    assert run(renderer, tmp_path, "one")["status"] == "ok"
    assert run(renderer, tmp_path, "two")["status"] == "ok"
    assert [sandboxed(argv) for argv in fake.starts(tmp_path / "bin")] == [True, True]
    assert renderer.sandbox_state() == "on"


def test_a_host_without_a_usable_sandbox_falls_back_once_and_says_so(renderer, tmp_path, monkeypatch):
    browser = fake.install(tmp_path / "bin", sandbox="broken")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(browser))
    assert run(renderer, tmp_path, "one")["status"] == "ok"
    assert run(renderer, tmp_path, "two")["status"] == "ok"
    # The first call tries the sandbox, then starts without it; later calls go straight there.
    assert [sandboxed(argv) for argv in fake.starts(tmp_path / "bin")] == [True, False, False]
    assert renderer.sandbox_state() == "off"


def test_a_first_call_that_comes_after_the_sandboxed_child_died_still_falls_back(
        renderer, tmp_path, monkeypatch):
    # A loaded host can deschedule the thread between the spawn and the first DevTools write.
    # By then the sandboxed child has exited, and the write fails with a broken pipe.
    browser = fake.install(tmp_path / "bin", sandbox="broken")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(browser))
    original = renderer._Child.open
    opened = []

    def late_open(self, *args, **kwargs):
        if not opened:
            time.sleep(1.5)
        opened.append(self.sandbox)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(renderer._Child, "open", late_open)
    assert run(renderer, tmp_path, "one")["status"] == "ok"
    assert [sandboxed(argv) for argv in fake.starts(tmp_path / "bin")] == [True, False]
    assert renderer.sandbox_state() == "off"


def test_the_render_result_names_the_sandbox_state(tmp_path, monkeypatch):
    import sb_fakes

    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    monkeypatch.setattr(sb_fakes.PLUGIN.renderer, "sandbox_state", lambda: "off")
    rendered = sb_fakes.call(sb_fakes.PLUGIN.shader_render, {"source": sb_fakes.SKY, "name": "sky"})
    assert rendered["success"] is True and rendered["browser_sandbox"] == "off"
