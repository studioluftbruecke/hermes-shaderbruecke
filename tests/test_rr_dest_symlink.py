"""Writes into a project folder (`dest_dir`) never follow a link out of that folder.

A cloned repo can carry a symlink, dangling or not, named like an export file. The export and the
share copy must refuse it instead of creating or replacing the link's target.
"""

import os
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sb_fakes  # noqa: E402
from sb_fakes import PLUGIN, call  # noqa: E402
from shaderbruecke import exporting  # noqa: E402

MATCH = {"method": "fake", "verdict": "match", "why": "faked", "checks": [], "diff": None, "frames": []}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db, base = sb_fakes.fresh_profile(tmp_path, monkeypatch)
    monkeypatch.setattr(exporting, "prove", lambda *args, **kwargs: dict(MATCH))
    shader_id = sb_fakes.new_shader(db, base)
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    return db, base, shader_id, project, outside


def export(db, base, shader_id, project, **kwargs):
    conn = sqlite3.connect(db)
    try:
        return exporting.export_shader(conn, base, shader_id, "three", dest_dir=str(project),
                                       settings=dict(sb_fakes.SETTINGS), **kwargs)
    finally:
        conn.close()


@pytest.mark.parametrize("overwrite", [False, True])
def test_export_refuses_a_dangling_symlink_in_dest_dir(setup, overwrite):
    db, base, shader_id, project, outside = setup
    (project / "SkyMaterial.js").symlink_to(outside / "planted.js")
    answer = export(db, base, shader_id, project, overwrite=overwrite)
    assert answer["success"] is False and "symlink" in answer["error"]
    assert not (outside / "planted.js").exists()
    assert os.path.islink(project / "SkyMaterial.js")


@pytest.mark.parametrize("overwrite", [False, True])
def test_export_refuses_a_symlink_to_an_existing_file(setup, overwrite):
    db, base, shader_id, project, outside = setup
    (outside / "keys").write_text("mine\n")
    (project / "SkyMaterial.js").symlink_to(outside / "keys")
    answer = export(db, base, shader_id, project, overwrite=overwrite)
    assert answer["success"] is False
    assert (outside / "keys").read_text() == "mine\n"


def test_overwrite_replaces_a_hard_link_instead_of_writing_through_it(setup):
    db, base, shader_id, project, outside = setup
    (outside / "keys").write_text("mine\n")
    os.link(outside / "keys", project / "SkyMaterial.js")
    answer = export(db, base, shader_id, project, overwrite=True)
    assert answer["success"] is True, answer
    assert (outside / "keys").read_text() == "mine\n"
    assert "ShaderMaterial" in (project / "SkyMaterial.js").read_text()


def test_overwrite_still_replaces_a_regular_file(setup):
    db, base, shader_id, project, _outside = setup
    (project / "SkyMaterial.js").write_text("old")
    assert export(db, base, shader_id, project)["success"] is False
    assert (project / "SkyMaterial.js").read_text() == "old"
    answer = export(db, base, shader_id, project, overwrite=True)
    assert answer["success"] is True and str(project / "SkyMaterial.js") in answer["copied_to"]
    assert (project / "SkyMaterial.js").read_text() != "old"


def test_a_symlink_appearing_after_the_check_is_not_followed(setup, monkeypatch):
    db, base, shader_id, project, outside = setup
    real = exporting.dest_decision

    def check_then_plant(*args, **kwargs):
        decision = real(*args, **kwargs)
        if not (project / "SkyMaterial.js").is_symlink():
            (project / "SkyMaterial.js").symlink_to(outside / "planted.js")
        return decision

    monkeypatch.setattr(exporting, "dest_decision", check_then_plant)
    answer = export(db, base, shader_id, project, overwrite=True)
    assert answer["success"] is False
    assert not (outside / "planted.js").exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_share_refuses_a_symlink_in_dest_dir(setup, overwrite):
    db, base, shader_id, project, outside = setup
    (project / "sky.shaderbruecke.json").symlink_to(outside / "planted.json")
    answer = call(PLUGIN.shader_library, {"action": "share", "id": shader_id, "dest_dir": str(project),
                                          "overwrite": overwrite})
    assert answer["success"] is False and "symlink" in answer["error"]
    assert not (outside / "planted.json").exists()


def test_share_still_writes_a_new_file(setup):
    db, base, shader_id, project, _outside = setup
    answer = call(PLUGIN.shader_library, {"action": "share", "id": shader_id, "dest_dir": str(project)})
    assert answer["success"] is True and answer["copied_to"] == str(project / "sky.shaderbruecke.json")
    assert (project / "sky.shaderbruecke.json").read_text().startswith("{")
