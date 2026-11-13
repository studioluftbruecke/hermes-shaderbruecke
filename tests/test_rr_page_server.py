"""The proof page server answers None for any URL it cannot map to a file, NUL bytes included."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import renderer  # noqa: E402


def test_a_nul_byte_in_the_path_is_refused_not_raised(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    serve = renderer.page_server(tmp_path)
    assert serve(f"{renderer.PAGE_ORIGIN}/index.html%00.js") is None
    assert serve(f"{renderer.PAGE_ORIGIN}/%00") is None
    assert serve(f"{renderer.PAGE_ORIGIN}/index.html")[0] == b"<p>hi</p>"
