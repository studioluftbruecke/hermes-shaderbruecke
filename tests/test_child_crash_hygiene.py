"""A crashing browser leaves no core dump where the gateway runs.

The kernel writes a core into the crashing process's working directory. A Chromium crash used to
drop ~28 MB into the gateway's (or the test runner's) current directory. The child now starts in
its throwaway profile with core dumps off.
"""

import importlib.util
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_renderer():
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer_core", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_browser_starts_in_its_profile_with_core_dumps_off(tmp_path, monkeypatch):
    renderer = load_renderer()
    dump = tmp_path / "child.txt"
    script = tmp_path / "fake-chromium"
    script.write_text(f"#!/bin/sh\n{{ pwd; ulimit -c; echo \"$HOME\"; echo \"$@\"; }} > '{dump}'\n",
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(script))
    monkeypatch.chdir(tmp_path)

    renderer.render("void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }", tmp_path / "out", timeout_s=5.0)

    cwd, core_limit, home, args = dump.read_text(encoding="utf-8").splitlines()[:4]
    assert cwd == home, "the child runs inside its throwaway profile"
    assert core_limit == "0"
    assert "--remote-debugging-pipe" in args and "--headless=new" in args
