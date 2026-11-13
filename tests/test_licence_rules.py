"""The licence rules: any licence while none is inherited;
an inherited NC or SA licence allows only itself or a later version of the same elements."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import library  # noqa: E402


def meta(**extra):
    base = {"id": "s1", "name": "dusk", "tags": [], "licence": None, "licence_inherited": None,
            "starred": False, "archived_at": None, "updated_at": "2026-11-09T09:00:00Z"}
    base.update(extra)
    return base


def test_any_licence_may_be_picked_when_none_is_inherited():
    for option in library.licence_options(meta()):
        assert option["allowed"], option
    assert library.licence_change_refusal(meta(), "Made-Up-1.0") is not None


def test_an_inherited_nc_licence_fixes_the_licence_to_itself_or_a_later_version():
    inherited = meta(licence="CC-BY-NC-SA-3.0", licence_inherited="CC-BY-NC-SA-3.0")
    allowed = {o["id"] for o in library.licence_options(inherited) if o["allowed"]}
    assert allowed == {"CC-BY-NC-SA-3.0", "CC-BY-NC-SA-4.0"}
    for refused in (None, "MIT", "CC0-1.0", "CC-BY-4.0", "CC-BY-NC-4.0"):
        reason = library.licence_change_refusal(inherited, refused)
        assert reason and "CC-BY-NC-SA-3.0" in reason, refused
    later = meta(licence_inherited="CC-BY-NC-SA-4.0")
    assert library.licence_change_refusal(later, "CC-BY-NC-SA-3.0") is not None


def test_only_nc_or_sa_claims_inherit():
    assert library.inherits("CC-BY-NC-SA-3.0") and library.inherits("CC-BY-SA-4.0")
    assert not library.inherits("MIT") and not library.inherits("CC-BY-4.0") and not library.inherits(None)
