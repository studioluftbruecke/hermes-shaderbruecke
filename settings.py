"""The plugin's settings, read fresh on every tool call and every dashboard request.

A change in the Desktop settings form then applies to the next call without a reload. Hermes keeps
plugin settings under ``plugins.entries.shaderbruecke.settings`` in the active profile's
``config.yaml``; `hermes_cli.config.load_config_readonly` is the public, cached loader for it.
Without Hermes (tests, a bare checkout) every setting has its default.
"""

from __future__ import annotations

PLUGIN = "shaderbruecke"

DEFAULTS = {"allow_project_writes": False, "credit_line": True, "keep_versions": 5,
            "author_name": "", "author_url": "", "starter_prompt": True}
TEXT_LIMIT = 120
KEEP_VERSIONS_RANGE = (1, 100)


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    low, high = KEEP_VERSIONS_RANGE
    return max(low, min(high, number))


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:TEXT_LIMIT]


def _url(value) -> str:
    text = _text(value)
    return text if text.startswith(("https://", "http://")) and " " not in text else ""


def normalise(raw: dict | None) -> dict:
    """Typed settings from a raw mapping: unknown keys dropped, bad values replaced by defaults."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "allow_project_writes": _bool(raw.get("allow_project_writes"), DEFAULTS["allow_project_writes"]),
        "credit_line": _bool(raw.get("credit_line"), DEFAULTS["credit_line"]),
        "keep_versions": _int(raw.get("keep_versions"), DEFAULTS["keep_versions"]),
        "author_name": _text(raw.get("author_name")),
        "author_url": _url(raw.get("author_url")),
        "starter_prompt": _bool(raw.get("starter_prompt"), DEFAULTS["starter_prompt"]),
    }


def _raw_settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
    except Exception:
        return {}
    try:
        config = load_config_readonly() or {}
    except Exception:
        return {}
    entry = ((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN)
    if not isinstance(entry, dict):
        return {}
    merged: dict = {}
    # `config` is the legacy subtree; `settings` wins, as in PluginContext.get_config.
    for key in ("config", "settings"):
        if isinstance(entry.get(key), dict):
            merged.update(entry[key])
    return merged


def read_settings() -> dict:
    """``{"allow_project_writes": bool, "credit_line": bool, "keep_versions": int, "author_name": str,
    "author_url": str, "starter_prompt": bool}``."""
    return normalise(_raw_settings())
