"""Fakes shared by the boundary tests: a render without a browser and a fresh profile.

A test puts its own folder on `sys.path` and imports this module as `sb_fakes`.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import shaderbruecke as PLUGIN  # noqa: E402
from shaderbruecke import renderer  # noqa: E402

SKY = ("// @slider sunHeight 0.0..1.0 = 0.4 step 0.01 \"Sun height\"\n"
       "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
       "  vec2 uv = fragCoord / iResolution.xy;\n"
       "  fragColor = vec4(uv.x, sunHeight + 0.2 * sin(iTime), uv.y, 1.0);\n"
       "}\n")

SETTINGS = {"keep_versions": 5, "allow_project_writes": False, "credit_line": True,
            "author_name": "", "author_url": "", "starter_prompt": True}


class FakeBundle:
    """`renderer.render_bundle` without a browser: writes frames, a sheet and a thumb."""

    def __init__(self):
        self.calls = 0

    def __call__(self, source, out_dir, **kwargs):
        self.calls += 1
        out_dir = pathlib.Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, t in enumerate(kwargs.get("times", (0.0, 1.0, 2.5))):
            png = out_dir / f"frame-t{t:g}.png"
            png.write_bytes(b"\x89PNG fake")
            frames.append({"t": t, "png_path": str(png), "luma_mean": 0.3 + 0.1 * index,
                           "luma_std": 0.2, "nan_fraction": 0.0,
                           "moved": None if index == 0 else 0.3, "draw_ms": 1.0})
        sheet = out_dir / "contact-sheet.png"
        sheet.write_bytes(b"\x89PNG fake")
        thumb = out_dir / renderer.THUMB_NAME
        thumb.write_bytes(b"RIFF fake")
        return renderer.BundleResult("ok", frames=frames, late=[{"t": 10.0, "moved": 0.3}],
                                     contact_sheet=str(sheet), thumb=str(thumb),
                                     flash={"lumas": [0.3] * 61, "reds": [0.0] * 61, "fps": 60.0})


def fresh_profile(tmp_path: pathlib.Path, monkeypatch, settings: dict | None = None):
    """An empty database and data dir wired into the plugin, with the browser faked.

    Returns ``(db_path, base)``; every `_storage()` call opens its own connection, as production does.
    """
    db = tmp_path / "data.db"
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr(PLUGIN, "_storage", lambda: (sqlite3.connect(db), base))
    monkeypatch.setattr(PLUGIN.settings_mod, "read_settings", lambda: dict(settings or SETTINGS))
    monkeypatch.setattr(renderer, "render_bundle", FakeBundle())
    return db, base


def new_shader(db, base, name: str = "sky", source: str = SKY) -> str:
    conn = sqlite3.connect(db)
    try:
        out = json.loads(PLUGIN._render(conn, base, source=source, name=name, keep_iterating=True))
    finally:
        conn.close()
    assert out.get("success"), out
    return out["id"]


def call(handler, args: dict) -> dict:
    return json.loads(handler(args))
