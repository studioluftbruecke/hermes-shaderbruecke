"""Attendance when Hermes' finite-turn marker is or is not readable.

A `chat -q` / `-z` turn the gateway admits carries no environment marker; its only signal is
`gateway.session_finite.finite_turn_required()`. When that cannot be read (a Hermes release moved
it, or it raises), nobody can say whether a person is there, so writes are refused (fail closed)
with a sentence that says so.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import exporting, unattended  # noqa: E402

SENTENCE = "this Hermes version does not say whether a person is present, so writes are refused; update the plugin"


def finite_module(monkeypatch, behaviour):
    module = types.ModuleType("gateway.session_finite")
    module.finite_turn_required = behaviour
    monkeypatch.setitem(sys.modules, "gateway.session_finite", module)


def test_a_turn_hermes_marks_as_not_finite_is_attended(monkeypatch):
    finite_module(monkeypatch, lambda: False)
    assert unattended.unattended_reason() is None


def test_a_turn_hermes_marks_as_finite_is_unattended(monkeypatch):
    finite_module(monkeypatch, lambda: True)
    assert unattended.unattended_reason() == "single-query"


def test_a_marker_that_raises_counts_as_unattended(monkeypatch):
    def broken():
        raise RuntimeError("moved")
    finite_module(monkeypatch, broken)
    assert unattended.unattended_reason() == "unknown"


def test_a_missing_marker_counts_as_unattended(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.session_finite", None)    # import raises ImportError
    assert unattended.unattended_reason() == "unknown"


def test_the_refusal_for_an_unreadable_marker_says_why(monkeypatch, tmp_path):
    decision = exporting.dest_decision(str(tmp_path), ["x.js"], reason="unknown")
    assert decision == {"ok": False, "error": SENTENCE}
    assert exporting.dest_decision(str(tmp_path), ["x.js"], reason="unknown", allow_writes=True)["ok"] is True


def test_cron_and_platform_signals_win_over_an_unreadable_marker(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.session_finite", None)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert unattended.unattended_reason() == "cron"


@pytest.mark.skipif(importlib.util.find_spec("gateway") is None, reason="Hermes is not importable here")
def test_the_real_marker_of_this_hermes_reads_as_attended_outside_a_finite_turn(monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway.session_finite", raising=False)
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    assert unattended.unattended_reason() is None
