"""The `chromium` marker: integration tests skip where no browser exists, they do not fail.

`scripts/check.sh` runs plain pytest, and the Mac host and catalog CI have no browser. A skipped
proof is not a passed proof, so the PR says which host ran the marked tests.
"""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line("markers", "chromium: needs a Chromium binary (skipped without one)")


def chromium_available() -> bool:
    spec = importlib.util.spec_from_file_location("shaderbruecke_renderer_conftest", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.find_chromium() is not None


def pytest_collection_modifyitems(config, items):
    if chromium_available():
        return
    skip = pytest.mark.skip(reason="no Chromium on this host; the proof ran elsewhere (see the PR)")
    for item in items:
        if "chromium" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def attended_session(monkeypatch):
    """Every test runs as an attended session unless it sets the signals itself.

    The test runner may itself run inside a cron job or a gateway session, and the unattended
    rule would then refuse writes the test expects.
    """
    for name in ("HERMES_CRON_SESSION", "HERMES_SESSION_PLATFORM", "HERMES_SINGLE_QUERY_SESSION",
                 "HERMES_PLUGIN_HOST_PROCESS"):
        monkeypatch.delenv(name, raising=False)
