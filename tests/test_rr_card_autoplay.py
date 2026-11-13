"""The chat card decides autoplay from the result's flash verdict, so every result that draws the
card carries `checks`: a render, a play and an import."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from sb_fakes import PLUGIN, call  # noqa: E402


def test_render_play_and_import_results_carry_the_flash_verdict(tmp_path, monkeypatch):
    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    rendered = call(PLUGIN.shader_render, {"source": sb_fakes.SKY, "name": "sky"})
    played = call(PLUGIN.shader_library, {"action": "play", "id": rendered["id"]})
    imported = call(PLUGIN.shader_library, {"action": "import", "source": sb_fakes.SKY, "licence": "MIT"})
    for answer in (rendered, played, imported):
        assert answer["success"] is True, answer
        assert answer["checks"]["flash"]["verdict"] == "none", answer
