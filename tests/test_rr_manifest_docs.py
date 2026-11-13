"""The manifest and the README describe the same settings, in the catalog's shape."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def settings() -> dict[str, str]:
    """Each config_schema key with its indented body."""
    block = MANIFEST.split("config_schema:\n", 1)[1]
    return dict(re.findall(r"(?m)^  ([a-z_]+):\n((?:    .*\n?)+)", block))


def test_every_setting_has_a_label_and_a_row_in_the_readme():
    found = settings()
    assert len(found) >= 6
    for name, body in found.items():
        assert re.search(r'(?m)^    label: "[^"]+"$', body), name
        assert f"| `{name}` |" in README, name


def test_the_author_url_rule_reads_the_same_in_both_places():
    assert "http:// or https://" in settings()["author_url"]
    row = next(line for line in README.splitlines() if line.startswith("| `author_url` |"))
    assert "http:// or https://" in row


def test_the_credit_line_is_spelled_as_the_header_writes_it():
    assert "Shaderbruecke'" not in MANIFEST and "Made with Shaderbrücke" in MANIFEST


def test_the_description_is_one_short_sentence():
    description = re.search(r'(?m)^description: "([^"]+)"', MANIFEST).group(1)
    assert len(description) <= 200 and description.count(". ") == 0


def test_readme_sections_follow_the_shared_shape():
    headings = re.findall(r"(?m)^## (.+)$", README)
    assert "Install and update" in headings and "License" in headings
    assert "Install" not in headings and "Licence" not in headings


def disclosures() -> str:
    """The disclosure section with line breaks folded, so a wrapped phrase still matches."""
    return " ".join(README.split("## Safety and disclosures", 1)[1].split("\n## ", 1)[0].split())


def test_disclosures_name_every_write_and_every_background_job():
    text = disclosures()
    assert "share(dest_dir=" in text and "<data>/shares/" in text.replace("`", "")
    assert "**Background work.**" in text and "thumbnail" in text and "daemon thread" in text
    assert "Nothing is served to a browser" not in text


def test_disclosures_match_the_vendored_files_and_the_desktop_calls():
    text = disclosures()
    vendored = sorted(p.name for p in (ROOT / "headless" / "vendor" / "three-r187" / "build").glob("*.js"))
    assert "four build files" in text and len(vendored) == 4, vendored
    assert "session.create" in text and "talks only to the plugin's own API" not in text


def test_the_skill_names_share_writes_and_background_refreshes():
    skill = (ROOT / "skills" / "shader-craft" / "SKILL.md").read_text(encoding="utf-8")
    assert "dest_dir" in skill.split("- `share`", 1)[1].split("\n- ", 1)[0]
    assert "background" in skill.lower() and "thumbnail" in skill
