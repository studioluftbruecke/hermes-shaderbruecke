"""`@shader:` (F39): target parsing, the expansion text (P17's rule), completions, and the shim's
profile scoping (P22) against a stand-in for Hermes' plugin-manager table."""

import pathlib
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import record, references  # noqa: E402

SOURCE = ("// @slider sunHeight 0.0..1.0 = 0.3 step 0.01 \"Sun height\"\n"
          "// @slider haze 0.0..2.0 = 1.0\n"
          "void mainImage(out vec4 fragColor, in vec2 fragCoord) { fragColor = vec4(sunHeight); }\n")


def _doc(shader_id="20261109-120000-000001-dusk", version=1, **extra):
    doc = {"id": shader_id, "version": version, "name": "dusk \u2014 thin clouds", "source": SOURCE,
           "sliders": record.parse_sliders(SOURCE), "tuned_values": {}, "channels": [],
           "provenance": {"origin": "agent", "url": None, "author": None, "licence": None, "parent": None},
           "checks": {"status": "ok", "flat": False, "flash": {"verdict": "none"},
                      "motion": {"verdict": "moving"}},
           "edited_by": "agent", "created_at": "2026-11-09T12:00:00Z", "files": {}}
    doc.update(extra)
    return doc


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "data.db")
    record.ensure_schema(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("target, parsed", [
    ("20261109-120000-000001-dusk", ("20261109-120000-000001-dusk", None)),
    ("20261109-120000-000001-dusk@v2", ("20261109-120000-000001-dusk", 2)),
    ("`20261109-120000-000001-dusk@v2`", ("20261109-120000-000001-dusk", 2)),
    ("20261109-120000-000001-dusk@v2.", ("20261109-120000-000001-dusk", 2)),
    ("../etc/passwd", None),
    ("Dusk@v2", None),
    ("dusk@2", None),
])
def test_targets_are_ids_with_an_optional_version(target, parsed):
    assert references.parse_target(target) == parsed


def test_the_expansion_carries_source_sliders_tuned_values_and_one_line_each(conn):
    record.save(conn, _doc(version=1))
    record.save(conn, _doc(version=2, tuned_values={"haze": 1.4}))
    text = references.expand_from(conn, "20261109-120000-000001-dusk@v2")
    assert "version 2 of 2" in text
    assert "- sunHeight: 0..1, default 0.3, step 0.01 (\"Sun height\")" in text
    assert "- haze: 0..2, default 1, tuned 1.4" in text
    assert SOURCE.strip() in text
    assert "Licence: none chosen (private)" in text
    assert "Lineage: original" in text
    assert "Check: ok, flash none, motion moving, not flat" in text


def test_an_older_version_is_expanded_as_itself(conn):
    record.save(conn, _doc(version=1, source="// v1 only\n" + SOURCE))
    record.save(conn, _doc(version=2))
    text = references.expand_from(conn, "20261109-120000-000001-dusk@v1")
    assert "version 1 of 2" in text and "// v1 only" in text


def test_unknown_targets_expand_to_nothing(conn):
    record.save(conn, _doc())
    assert references.expand_from(conn, "20261109-120000-000001-dusk@v9") is None
    assert references.expand_from(conn, "nope") is None
    assert references.expand_from(conn, "not an id") is None


def test_completions_match_names_and_show_them_with_a_hyphen(conn):
    record.save(conn, _doc(version=3))
    record.save(conn, _doc(shader_id="20261109-110000-000002-ocean", name="ocean"))
    rows = references.complete_from(conn, "thin", 10)
    assert rows == [("20261109-120000-000001-dusk@v3", "dusk - thin clouds", "v3, 2026-11-09T12:00:00Z")]
    assert len(references.complete_from(conn, "", 10)) == 2


def _install_fake_hermes(monkeypatch, home, managers):
    fake = ModuleType("hermes_cli.plugins")
    fake._plugin_home_key = lambda: home
    fake._plugin_managers_by_home = managers
    package = ModuleType("hermes_cli")
    package.plugins = fake
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", fake)
    monkeypatch.setattr(references, "_profile_enables_plugin", lambda: True)


def _loaded(module, *, enabled=True, name="shaderbruecke"):
    return SimpleNamespace(manifest=SimpleNamespace(name=name), enabled=enabled, module=module)


def test_the_shim_resolves_the_running_profiles_module(monkeypatch):
    new = SimpleNamespace(references=SimpleNamespace(expand=lambda t: "new code"))
    old = SimpleNamespace()  # v0.1.1: no `references`
    managers = {"/p/sb-v02": SimpleNamespace(_plugins={"shaderbruecke": _loaded(new)}),
                "/p/coding": SimpleNamespace(_plugins={"shaderbruecke": _loaded(old)}),
                "/p/other": SimpleNamespace(_plugins={})}
    _install_fake_hermes(monkeypatch, "/p/sb-v02", managers)
    assert references.running_module() is new
    _install_fake_hermes(monkeypatch, "/p/coding", managers)
    assert getattr(references.running_module(), "references", None) is None
    _install_fake_hermes(monkeypatch, "/p/other", managers)
    assert references.running_module() is None
    _install_fake_hermes(monkeypatch, "/p/never-loaded", managers)
    assert references.running_module() is None


def test_a_disabled_plugin_is_not_resolved(monkeypatch):
    managers = {"/p/x": SimpleNamespace(_plugins={"shaderbruecke": _loaded(object(), enabled=False)})}
    _install_fake_hermes(monkeypatch, "/p/x", managers)
    assert references.running_module() is None


def test_outside_hermes_there_is_no_shim():
    assert references.make_shim() is None


def test_the_config_is_read_again_only_when_it_changed(tmp_path, monkeypatch):
    import os

    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled: [shaderbruecke]\n")
    fake = ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    fake_yaml = ModuleType("hermes_yaml")
    fake_yaml.safe_load = lambda text: {"plugins": {"enabled": ["shaderbruecke"] if "shaderbruecke" in text else []}}
    monkeypatch.setitem(sys.modules, "hermes_yaml", fake_yaml)
    reads = []
    real_read = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, *a, **k: (reads.append(self), real_read(self, *a, **k))[1])
    assert references._profile_enables_plugin() and references._profile_enables_plugin()
    assert len(reads) == 1
    config.write_text("plugins:\n  enabled: []\n")
    stat = config.stat()
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert references._profile_enables_plugin() is False and len(reads) == 2
