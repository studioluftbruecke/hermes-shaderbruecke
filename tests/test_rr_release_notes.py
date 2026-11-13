"""The release notes and README say what the code does, and carry no dated work notes."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTES = ("README.md", "CHANGELOG.md")
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def lines(name):
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def paragraphs(name):
    return [" ".join(part.split()) for part in re.split(r"\n\s*\n|\n(?=- )", (ROOT / name).read_text(encoding="utf-8"))]


def test_no_dated_work_notes():
    dated = [f"{name}:{number}: {line.strip()}" for name in NOTES
             for number, line in enumerate(lines(name), 1) if re.search(r"\b20\d\d-\d\d-\d\d\b", line)]
    assert not dated, "\n".join(dated)


def test_the_sandbox_field_is_promised_only_where_the_code_sets_it():
    # Only a render that becomes a new version carries `browser_sandbox`; failures and the
    # unchanged-source answer do not.
    claims = [text for name in NOTES for text in paragraphs(name)
              if "browser_sandbox" in text or "render result says which" in text]
    assert claims
    for text in claims:
        assert not re.search(r"\bevery\b[^.;]*\bresult", text), text
        assert "new version" in text, text


def test_the_thumbnail_line_names_how_many_batches_run_at_once():
    script = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    match = re.search(r"const THUMB_PARALLEL = (\d+)", script)
    assert match
    parallel = int(match.group(1))
    claims = [text for text in paragraphs("CHANGELOG.md") if "thumbnail batches" in text]
    assert claims
    for text in claims:
        assert "all thumbnail batches at once" not in text, text
        assert f"{NUMBER_WORDS[parallel]} at a time" in text, text
