"""Does this turn run without a human? The plugin's own copy of Hermes' unattended rule.

`tools/approval_context.py` is not a public import, so the rule is restated here and kept to the
signals Hermes itself uses: a cron session, a programmatic gateway platform, or a single query run
(`hermes chat -q` / `-z`). A human is never asked to approve anything at that point, so an action
that needs a person must refuse instead of hanging.

Under `plugins.isolation: host` the tools run in a separate plugin host, where the session
variables and the environment are the host's own, not the calling turn's. There the rule reads what
Hermes forwards through `ctx` (`current_cron_execution()`, `session_surface()`), and on a CLI
surface, where a one-shot run cannot be told from a chat, it fails closed (Coolify Cockpit's B1).
"""

from __future__ import annotations

import os

_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
_TRUTHY = {"1", "true", "yes", "on"}
# Surfaces where a one-shot `hermes -z` / `chat -q` turn can run.
_HOST_BLIND_SURFACES = frozenset({"", "cli", "oneshot"})
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"


def _session_env(name: str) -> str:
    """Session-scoped value (contextvar first, so one cron job cannot taint the gateway), then env."""
    try:
        from gateway.session_context import get_session_env
        value = get_session_env(name, "")
        if value:
            return value
    except Exception:
        pass
    return os.environ.get(name, "")


def _finite_turn() -> bool:
    """Hermes marks `chat -q` / `-z` turns as finite; readable in-process only."""
    try:
        from gateway.session_finite import finite_turn_required
        return finite_turn_required() is True
    except Exception:
        return False


def _ctx_call(ctx, name: str):
    try:
        return getattr(ctx, name)() if ctx is not None and hasattr(ctx, name) else None
    except Exception:
        return None


def unattended_reason(ctx=None) -> str | None:
    """None when a person can answer; otherwise the signal that says nobody is there."""
    if _ctx_call(ctx, "current_cron_execution") is not None \
            or _session_env("HERMES_CRON_SESSION").strip().lower() in _TRUTHY:
        return "cron"
    platform = str(_ctx_call(ctx, "session_surface") or _session_env("HERMES_SESSION_PLATFORM")).strip().lower()
    if platform in _UNATTENDED_PLATFORMS:
        return platform
    if _finite_turn() or _session_env("HERMES_SINGLE_QUERY_SESSION").strip().lower() in _TRUTHY:
        return "single-query"
    if os.environ.get(HOST_PROCESS_ENV, "").strip().lower() in _TRUTHY and platform in _HOST_BLIND_SURFACES:
        return "plugin-host"
    return None


def is_unattended(ctx=None) -> bool:
    return unattended_reason(ctx) is not None
