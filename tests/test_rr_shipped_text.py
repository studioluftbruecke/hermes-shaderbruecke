"""Shipped files name behaviour, not internal plan ids, people or paths that do not ship.

A stranger maintaining the public repository cannot resolve a milestone id or a link into the
private workbench, so these fail here instead of in a reviewer's first read. The template-pinned
test files and the vendored three.js are left out; CHANGELOG is the release notes.
"""

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP = re.compile(r"^(headless/vendor/|CHANGELOG\.md$|tests/test_sidebar_row\.py$|tests/test_manifest_matches\.py$)"
                  r"|\.png$")
# Built from pieces, so this file does not match itself.
PATTERNS = [
    "experi" + r"ments/", "Konst" + "antin", r"\bconst" + r"raint \d", r"v0\d-con" + "tract",
    r"(?<![\w-])(?:F|P|D|LB|K-S|I)\d{1,2}(?:\.\d)?(?=[):,\s])", r"\bprobe " + r"P\d", r"live " + "box",
]
RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS))


def shipped_files() -> list[str]:
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        listed = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
    return [name for name in listed if not SKIP.search(name) and pathlib.Path(name).name != pathlib.Path(__file__).name]


def test_no_plan_ids_names_or_private_paths_in_shipped_files():
    found = []
    for name in shipped_files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if RX.search(line):
                found.append(f"{name}:{number}: {line.strip()[:120]}")
    assert not found, "\n".join(found)
