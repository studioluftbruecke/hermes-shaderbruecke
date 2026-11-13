"""Runs the Desktop half's node suites that use `tests/desktop_harness.mjs` under pytest, so
`scripts/check.sh` covers them. Skipped where no node is installed: a skipped run is not a passed
one, and the PR names the host that ran it."""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SUITES = ["test_desktop_library_pages.mjs", "test_desktop_player.mjs", "test_desktop_share.mjs",
          "test_desktop_thumbs_load.mjs"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the Desktop half's module")
@pytest.mark.parametrize("suite", SUITES)
def test_desktop_suite_in_node(suite, tmp_path):
    proc = subprocess.run(["node", str(HERE / suite)], capture_output=True, text=True, timeout=120,
                          env={"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path)})
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    failed = [line for line in lines if line.get("ok") is False]
    assert proc.returncode == 0 and not failed and lines, failed or proc.stderr[-2000:]


def test_every_harness_suite_is_listed():
    found = sorted(path.name for path in HERE.glob("test_desktop_*.mjs")
                   if "desktop_harness.mjs" in path.read_text(encoding="utf-8"))
    assert found == sorted(SUITES)
