"""library.py: states and the autoplay rule, rows, filters, sort and paging, compare, tags and names."""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import library  # noqa: E402


def doc(**extra):
    base = {"id": "s1", "version": 2, "name": "dusk", "source": "a\nb\nc\n",
            "sliders": [{"name": "glow", "min": 0, "max": 1, "default": 0.5, "step": None}],
            "provenance": {"origin": "agent", "licence": None, "parent": None},
            "checks": {"status": "ok", "flat": False, "flash": {"verdict": "none"}},
            "created_at": "2026-11-09T10:00:00Z", "files": {}}
    base.update(extra)
    return base


def meta(**extra):
    base = {"id": "s1", "name": "dusk", "tags": [], "licence": None, "licence_inherited": None,
            "starred": False, "archived_at": None, "updated_at": "2026-11-09T09:00:00Z"}
    base.update(extra)
    return base


@pytest.mark.parametrize("checks, expected, plays", [
    ({"status": "ok", "flat": False, "flash": {"verdict": "none"}}, [], True),
    ({"status": "ok", "flat": True, "flash": {"verdict": "none"}}, ["flat"], True),
    ({"status": "ok", "flat": False, "flash": {"verdict": "warn"}}, ["flash_warn"], False),
    ({"status": "ok", "flat": False, "flash": {"verdict": "block"}}, ["flash_block"], False),
    ({"status": "ok", "flat": False}, ["legacy"], False),
])
def test_states_and_the_autoplay_rule(checks, expected, plays):
    assert library.states(doc(checks=checks), has_thumb=True) == expected
    assert library.autoplay(doc(checks=checks)) is plays


def test_missing_thumbs_and_pruned_frames_are_states(tmp_path):
    gone = str(tmp_path / "contact-sheet.png")
    assert library.states(doc(files={"contact_sheet": gone}), has_thumb=False) == ["no_thumb", "frames_pruned"]


def test_a_row_carries_meta_lineage_and_shows_dashes_as_hyphens():
    r = library.row(doc(provenance={"origin": "agent", "parent": "p1", "parent_version": 3}),
                    meta(name="dusk \u2014 clouds", tags=["sky"], starred=True, licence="MIT"),
                    versions=2, thumb="data:image/webp;base64,AA", created="2026-11-01T00:00:00Z",
                    parent={"id": "p1", "name": "ocean", "version": 3})
    assert r["name"] == "dusk - clouds" and r["versions"] == 2 and r["starred"] and r["tags"] == ["sky"]
    assert r["updatedAt"] == "2026-11-09T10:00:00Z" and r["createdAt"] == "2026-11-01T00:00:00Z"
    assert r["parent"]["version"] == 3 and r["authorKind"] == "agent" and r["autoplay"]


def rows():
    out = []
    for index, (name, starred, archived, tags, licence, states) in enumerate([
        ("alpha", False, False, ["sky"], None, []),
        ("bravo", True, False, [], "MIT", ["flat"]),
        ("charlie", False, True, ["sky"], None, []),
        ("delta", False, False, ["sea"], "CC-BY-4.0", ["no_thumb"]),
    ]):
        out.append({"id": f"s{index}", "name": name, "starred": starred, "archived": archived,
                    "tags": tags, "licence": licence, "states": states,
                    "updatedAt": f"2026-11-0{index + 1}T00:00:00Z", "createdAt": f"2026-10-0{4 - index}T00:00:00Z"})
    return out


def names(query):
    window, _total, _cursor = library.page(rows(), library.parse_query(query))
    return [r["name"] for r in window]


def test_filters_sort_and_starred_first():
    assert names({}) == ["bravo", "delta", "alpha"]                 # starred first, then last edited
    assert names({"sort": "name"}) == ["bravo", "alpha", "delta"]
    assert names({"sort": "created"}) == ["bravo", "alpha", "delta"]
    assert names({"archived": "1"}) == ["charlie"]
    assert names({"q": "alp"}) == ["alpha"]
    assert names({"tag": "sky"}) == ["alpha"]
    assert names({"state": "ok"}) == ["alpha"]
    assert names({"state": "flat"}) == ["bravo"]
    assert names({"licence": "private"}) == ["alpha"]
    assert names({"licence": "MIT"}) == ["bravo"]
    assert names({"starred": "1"}) == ["bravo"]
    assert names({"state": "nonsense", "sort": "nonsense"}) == ["bravo", "delta", "alpha"]


def test_the_cursor_pages_through_the_selection():
    query = library.parse_query({"limit": "2"})
    first, total, cursor = library.page(rows(), query)
    assert total == 3 and cursor == "2" and len(first) == 2
    second, _total, end = library.page(rows(), library.parse_query({"limit": "2", "cursor": cursor}))
    assert [r["name"] for r in second] == ["alpha"] and end is None


def test_pick_up_ignores_filters_and_archived_shaders():
    assert library.pick_up(rows())["name"] == "delta"
    assert library.facets(rows()) == {"tags": ["sea", "sky"], "licences": ["CC-BY-4.0", "MIT", "private"]}


def test_tags_and_names_are_cleaned_or_refused():
    assert library.clean_tags([" Sky ", "sky", "Deep Sea"]) == ["sky", "deep-sea"]
    assert "at most 8" in library.clean_tags([f"t{i}" for i in range(9)])
    assert "longer than 24" in library.clean_tags(["x" * 25])
    assert library.clean_tags("sky") == "tags must be a list of strings"
    assert library.clean_name("  dusk \n sky ") == "dusk sky"
    assert library.clean_name("   ") is None


def test_compare_gives_a_line_diff_and_slider_changes():
    a = doc(source="one\ntwo\nthree", sliders=[{"name": "glow", "min": 0, "max": 1, "default": 0.5},
                                              {"name": "old", "min": 0, "max": 1, "default": 0.1}])
    b = doc(source="one\nTWO\nthree\nfour", sliders=[{"name": "glow", "min": 0, "max": 2, "default": 0.5},
                                                     {"name": "new", "min": 0, "max": 1, "default": 0.2}])
    diff = library.compare(a, b)
    assert [(l["op"], l["text"]) for l in diff["lines"]] == [
        (" ", "one"), ("-", "two"), ("+", "TWO"), (" ", "three"), ("+", "four")]
    assert {s["name"]: s["change"] for s in diff["sliders"]} == {"glow": "changed", "old": "removed", "new": "added"}
