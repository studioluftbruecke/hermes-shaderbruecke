"""Does this turn run without a human? The plugin's own copy of Hermes' unattended rule.

`tools/approval_context.py` is not a public import, so the rule is restated here with the signals
Hermes itself uses. A turn is unattended when:

- it is a cron run (`ctx.current_cron_execution()` or `HERMES_CRON_SESSION`);
- the session platform is `webhook`, `msgraph_webhook` or `api_server`;
- it is a single query (`HERMES_SINGLE_QUERY_SESSION`, or Hermes' finite-turn marker for a
  `hermes chat -q` / `-z` turn the gateway admitted);
- inside the plugin host (`HERMES_PLUGIN_HOST_PROCESS=1`, `plugins.isolation: host`), the
  forwarded surface is one a one-shot run uses (empty, `cli`, `oneshot`): the host's own
  environment cannot tell such a run from a chat, so it fails closed.

When the finite-turn marker cannot be read, nobody can say whether a person is present, and the
turn counts as unattended too (`UNKNOWN`). A human is never asked to approve anything in an
unattended turn, so an action that needs a person refuses instead of hanging.
"""

from __future__ import annotations

import os

_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
_TRUTHY = {"1", "true", "yes", "on"}
# Surfaces where a one-shot `hermes -z` / `chat -q` turn can run.
_HOST_BLIND_SURFACES = frozenset({"", "cli", "oneshot"})
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
UNKNOWN = "unknown"
UNKNOWN_REFUSAL = ("this Hermes version does not say whether a person is present, so writes are refused; "
                   "update the plugin")


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


def _finite_turn() -> bool | None:
    """Hermes marks `chat -q` / `-z` turns as finite; None when the marker cannot be read."""
    try:
        # Private on purpose: Hermes 0.23.1 has no public signal for a gateway-admitted -q/-z turn.
        from gateway.session_finite import finite_turn_required
        return finite_turn_required() is True
    except Exception:
        return None


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
    if _session_env("HERMES_SINGLE_QUERY_SESSION").strip().lower() in _TRUTHY:
        return "single-query"
    finite = _finite_turn()
    if finite is None:
        return UNKNOWN
    if finite:
        return "single-query"
    if os.environ.get(HOST_PROCESS_ENV, "").strip().lower() in _TRUTHY and platform in _HOST_BLIND_SURFACES:
        return "plugin-host"
    return None


def is_unattended(ctx=None) -> bool:
    return unattended_reason(ctx) is not None
