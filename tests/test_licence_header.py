"""Every export target writes the version's own licence into its header: after a
picker change the latest version exports with the new licence, an earlier version keeps its own,
and the header shows the shader's current name and the author from the settings."""

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import exporting, record  # noqa: E402
from shaderbruecke.exporters import registry  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SKY = (FIXTURES / "sky.glsl").read_text()
RAW_TSL = (FIXTURES / "expected" / "sky.transpiler-raw.txt").read_text()


def version(number: int, licence: str | None, **extra) -> dict:
    doc = {"id": "20261109-120000-000001-sky", "version": number, "name": "sky",
           "source": SKY, "sliders": record.parse_sliders(SKY), "tuned_values": {}, "channels": [],
           "provenance": {"origin": "agent", "url": None, "author": None, "licence": licence, "parent": None},
           "checks": {"status": "ok"}, "edited_by": "agent", "author_kind": "agent",
           "created_at": "2026-11-09T12:00:00Z", "files": {}}
    doc.update(extra)
    return doc


def main_text(target: str, doc: dict) -> str:
    row = registry.EXPORT_TARGETS[target]
    options = {"credit": True, "raw": RAW_TSL}
    files = row.files(doc, options)
    name = row.main_file.format(name=exporting.pascal(doc["name"]))
    return files[name]


@pytest.mark.parametrize("target", registry.target_ids())
def test_the_header_carries_the_versions_licence(target):
    assert "Licence: CC-BY-4.0" in main_text(target, version(2, "CC-BY-4.0"))
    assert "Licence: unknown (private; the author has not chosen one)" in main_text(target, version(1, None))


@pytest.mark.parametrize("target", registry.target_ids())
def test_after_a_picker_change_only_the_latest_version_exports_the_new_licence(target, tmp_path):
    conn = sqlite3.connect(tmp_path / "data.db")
    record.ensure_schema(conn)
    record.save(conn, version(1, None))
    record.save(conn, version(2, None))
    record.set_licence(conn, tmp_path / "data", "20261109-120000-000001-sky", "CC-BY-SA-4.0")
    meta = record.get_meta(conn, "20261109-120000-000001-sky")
    latest = exporting.export_view(record.get_version(conn, "20261109-120000-000001-sky", 2), meta)
    earlier = exporting.export_view(record.get_version(conn, "20261109-120000-000001-sky", 1), meta)
    assert "Licence: CC-BY-SA-4.0" in main_text(target, latest)
    assert "CC-BY-SA-4.0" not in main_text(target, earlier)


@pytest.mark.parametrize("target", registry.target_ids())
def test_a_rename_and_the_author_setting_reach_the_header(target):
    view = exporting.export_view(version(1, "MIT"), {"name": "Dusk sky"},
                                 {"author_name": "Ada Example", "author_url": "https://example.org"})
    text = main_text(target, view)
    assert "Dusk sky" in text and "Ada Example (https://example.org)" in text


def test_an_imported_shader_keeps_its_original_author():
    imported = version(1, "CC-BY-NC-SA-3.0", provenance={"origin": "import", "author": "iq", "url": "https://x",
                                                         "licence": "CC-BY-NC-SA-3.0", "parent": None})
    view = exporting.export_view(imported, {"name": "sky"}, {"author_name": "Ada Example"})
    assert view["provenance"]["author"] == "iq"
