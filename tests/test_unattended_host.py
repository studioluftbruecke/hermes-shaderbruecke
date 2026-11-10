"""The unattended rule under `plugins.isolation: host` (probe 2026-11-08).

In a separate plugin host the session variables and the environment are the host's own, not the
calling turn's, so `hermes -z`, cron and the API server all looked like a chat with a person and
`shader_export(dest_dir=...)` would write into a project unattended. Coolify Cockpit hit the same
blind spot (its bug B1). The rule now reads what Hermes forwards through `ctx`
(`current_cron_execution()`, `session_surface()`) and, inside the host on a CLI surface where a
one-shot run cannot be told from a chat, refuses (fail closed).
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import unattended  # noqa: E402


class FakeCtx:
    def __init__(self, surface="", cron=None):
        self.surface, self.cron = surface, cron

    def session_surface(self):
        return self.surface

    def current_cron_execution(self):
        return self.cron


def test_in_process_cli_chat_is_attended(monkeypatch):
    assert unattended.unattended_reason(FakeCtx("cli")) is None


def test_a_forwarded_cron_execution_is_unattended(monkeypatch):
    assert unattended.unattended_reason(FakeCtx("cli", cron={"job": "x"})) == "cron"


def test_a_forwarded_api_server_surface_is_unattended(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert unattended.unattended_reason(FakeCtx("api_server")) == "api_server"


def test_inside_the_host_a_cli_surface_fails_closed(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    for surface in ("", "cli", "oneshot"):
        assert unattended.unattended_reason(FakeCtx(surface)) == "plugin-host", surface


def test_inside_the_host_a_desktop_chat_is_attended(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert unattended.unattended_reason(FakeCtx("desktop")) is None


def test_shader_export_reads_the_registered_ctx(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    seen = {}

    class Ctx(FakeCtx):
        def register_tool(self, **kwargs):
            pass

        def register_skill(self, *args, **kwargs):
            pass

    PLUGIN.register(Ctx("oneshot"))

    def fake_export(*args, **kwargs):
        seen["reason"] = kwargs["unattended_reason"]
        return {"success": False}

    monkeypatch.setattr(PLUGIN.exporting, "export_shader", fake_export)
    import sqlite3
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(tmp_path / "d.db"), tmp_path))
    PLUGIN.shader_export({"id": "sky", "target": "three", "dest_dir": str(tmp_path)})
    assert seen["reason"] == "plugin-host"
