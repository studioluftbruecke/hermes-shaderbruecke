"""Static rules for desktop/plugin.js that a maintainer checks by reading it."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_JS = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def test_navigation_goes_through_the_sdk_only():
    assert "sdk.host.navigate(" in PLUGIN_JS
    assert not re.search(r"window\.location\.hash\s*=", PLUGIN_JS)
