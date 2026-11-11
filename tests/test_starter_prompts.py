"""The New shader button picks one of the starter prompts in desktop/plugin.js (V4).

The list is JSON between two marker comments, so this test reads it the way a person would see it
in the file and holds it to the rules: a hundred distinct one-sentence prompts that each ask for
sliders, fit the composer, and never ask for a flashing shader (which the plugin's own flash check
would hold back on the first render).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_JS = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"
FLASHY = ("strobe", "flash", "flicker", "blink", "lightning")


def _prompts() -> list[str]:
    text = PLUGIN_JS.read_text(encoding="utf-8")
    block = re.search(r"// sb:starter-prompts:begin\n(.*?)// sb:starter-prompts:end", text, re.S)
    assert block, "the starter prompt markers are missing from desktop/plugin.js"
    body = block.group(1).strip()
    prefix = "const STARTER_PROMPTS = "
    assert body.startswith(prefix), "the block holds one `const STARTER_PROMPTS = [...]`"
    return json.loads(body[len(prefix):])


def test_one_hundred_distinct_prompts():
    prompts = _prompts()
    assert len(prompts) == 100
    assert len({p.strip().lower() for p in prompts}) == 100


def test_each_prompt_is_short_and_asks_for_sliders():
    for prompt in _prompts():
        assert isinstance(prompt, str) and prompt == prompt.strip()
        assert len(prompt) <= 140, prompt
        assert "slider" in prompt.lower(), prompt
        assert "\u2013" not in prompt and "\u2014" not in prompt, prompt


def test_no_prompt_asks_for_flashing():
    for prompt in _prompts():
        lowered = prompt.lower()
        assert not any(word in lowered for word in FLASHY), prompt
