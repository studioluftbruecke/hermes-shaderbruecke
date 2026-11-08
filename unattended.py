"""Does this turn run without a human? The plugin's own copy of Hermes' unattended rule.

`tools/approval_context.py` is not a public import, so the rule is restated here and kept to the
three signals Hermes itself uses: a cron session, a programmatic gateway platform, or a single
query run (`hermes chat -q`). A human is never asked to approve anything at that point, so an
action that needs a person must refuse instead of hanging.
"""

from __future__ import annotations

import os

_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
_TRUTHY = {"1", "true", "yes", "on"}


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


def unattended_reason() -> str | None:
    """None when a person can answer; otherwise the signal that says nobody is there."""
    if _session_env("HERMES_CRON_SESSION").strip().lower() in _TRUTHY:
        return "cron"
    platform = _session_env("HERMES_SESSION_PLATFORM").strip().lower()
    if platform in _UNATTENDED_PLATFORMS:
        return platform
    if _session_env("HERMES_SINGLE_QUERY_SESSION").strip().lower() in _TRUTHY:
        return "single-query"
    return None


def is_unattended() -> bool:
    return unattended_reason() is not None
