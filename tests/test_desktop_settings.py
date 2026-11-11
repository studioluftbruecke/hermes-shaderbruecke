"""Runs `tests/test_desktop_settings.mjs` (the settings page titled Shaderbrücke, the sidebar row at
52 through the shared module, the way to the folded Agent settings) under pytest, so
`scripts/check.sh` covers it. Skipped where no node is installed: a skipped run is not a passed one,
and the PR names the host that ran it."""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the Desktop half's module")
def test_the_settings_page_and_the_sidebar_row_in_node(tmp_path):
    proc = subprocess.run(["node", str(HERE / "test_desktop_settings.mjs")], capture_output=True, text=True,
                          timeout=60, env={"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path)})
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    failed = [line for line in lines if line.get("ok") is False]
    assert proc.returncode == 0 and not failed and len(lines) == 9, failed or proc.stderr[-2000:] or lines
