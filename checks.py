"""Pure checks for a Shadertoy-style shader: lint, frame statistics, flash verdict.

Nothing here starts a browser or imports Hermes, so the tests run on any host and the same
algorithms can be mirrored in `desktop/plugin.js`.

The lint reads its rules from `lint_rules.json`, one table shared with the Desktop half. Two rule
kinds exist because a negative condition cannot be one regular expression:

``regex``
    A pattern applied to the source with comments blanked out (comments keep their newlines, so
    line numbers stay the ones the agent wrote). A `while (true)` inside a comment must never
    block a render: the skill asks for heavily commented shaders.
``for_bound``
    A small parse: every ``for`` statement's middle clause must compare the counter against a
    literal or a ``const`` declared in the same source, so the driver can bound the loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

RULES_PATH = Path(__file__).with_name("lint_rules.json")

_FLASH_STEP = 0.10          # WCAG 2.3.1: a flash is a pair of opposing changes of >= 10% luminance
_FLASH_DARK = 0.80          # ... where the darker of the two states is below 80% relative luminance
_RED_STEP = 0.20            # saturated-red share change that counts as a red flash
_WINDOW = 1.0               # seconds


@dataclass(frozen=True)
class LintFinding:
    rule: str
    line: int
    severity: str           # "error" | "warn"
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "line": self.line, "severity": self.severity,
                "message": self.message, "hint": self.hint}


def load_rules(path: Path | str | None = None) -> list[dict]:
    data = json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    rules = data["rules"] if isinstance(data, dict) else data
    return [dict(rule) for rule in rules]


# Read once, with the code. A gateway keeps these modules until a plugin reload, so a rules file
# read per call could come from newer files on disk and name a kind this code cannot run.
RULES = load_rules()


def strip_comments(source: str) -> str:
    """Blank out ``//`` and ``/* */`` comments with spaces, keeping every newline.

    Newlines survive so a finding's line number is the agent's line number.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if char == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif char == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _line_at(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _for_clauses(text: str) -> list[tuple[int, list[str]]]:
    """Every ``for`` header as (index of ``for``, top-level ``;``-separated clauses)."""
    found: list[tuple[int, list[str]]] = []
    for match in re.finditer(r"\bfor\s*\(", text):
        start = text.index("(", match.start())
        depth, i = 0, start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[start + 1:i]
        clauses: list[str] = []
        depth, current = 0, []
        for char in body:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == ";" and depth == 0:
                clauses.append("".join(current))
                current = []
            else:
                current.append(char)
        clauses.append("".join(current))
        found.append((match.start(), clauses))
    return found


def _const_names(text: str) -> set[str]:
    return set(re.findall(r"\bconst\s+\w+\s+([A-Za-z_]\w*)\s*=", text))


_BOUND_RE = re.compile(r"^\s*[A-Za-z_]\w*\s*(<=|>=|<|>)\s*([0-9]+(?:\.[0-9]*)?|[A-Za-z_]\w*)\s*$")

# The names `@slider` lines claim. record.parse_sliders is stricter (range, default, cap); the
# renderer passes its parsed names, and this loose read is for callers with only the source.
_SLIDER_NAME_RE = re.compile(r"^[ \t]*//[ \t]*@slider[ \t]+([A-Za-z_]\w*)", re.M)
_DECL_TYPE = r"(?:float|int|uint|bool|[biu]?vec[234]|mat[234](?:x[234])?)"


def slider_names_in(source: str) -> list[str]:
    return list(dict.fromkeys(_SLIDER_NAME_RE.findall(source)))


def _slider_declarations(text: str, names: Iterable[str]) -> list[tuple[str, int, str]]:
    """(name, index, scope) for each declaration of a slider name. Parameters are skipped: a
    parameter named like a slider and called with it is fine. `#define` counts as global."""
    found = []
    for name in names:
        pattern = re.compile(rf"(?:\b{_DECL_TYPE}[ \t]+|#[ \t]*define[ \t]+){re.escape(name)}\b")
        for match in pattern.finditer(text):
            before = text[:match.start()]
            if before.count("(") > before.count(")"):
                continue
            nested = before.count("{") > before.count("}")
            global_scope = match.group(0).lstrip().startswith("#") or not nested
            found.append((name, match.start(), "global" if global_scope else "local"))
    return found


def lint(source: str, rules: Sequence[dict] | None = None,
         slider_names: Sequence[str] | None = None) -> list[LintFinding]:
    """Every finding for `source`, ordered by line. Comments are stripped first, always.

    `slider_names` are the sliders the renderer will declare as uniforms; by default the names on
    the source's `@slider` lines."""
    stripped = strip_comments(source)
    constants = _const_names(stripped)
    names = list(slider_names) if slider_names is not None else slider_names_in(source)
    findings: list[LintFinding] = []
    for rule in rules if rules is not None else RULES:
        kind = rule.get("kind", "regex")
        severity = rule.get("severity", "error")
        if kind == "regex":
            pattern = re.compile(rule["pattern"])
            for match in pattern.finditer(stripped):
                findings.append(LintFinding(rule["id"], _line_at(stripped, match.start()), severity,
                                            rule.get("message", rule["id"]), rule.get("hint", "")))
        elif kind == "for_bound":
            for index, clauses in _for_clauses(stripped):
                if len(clauses) < 2:
                    continue
                clause = clauses[1]
                match = _BOUND_RE.match(clause)
                bound = match.group(2) if match else None
                bounded = bool(match) and (bound[0].isdigit() or bound in constants)
                if not bounded:
                    findings.append(LintFinding(rule["id"], _line_at(stripped, index), severity,
                                                rule.get("message", "unbounded loop"),
                                                rule.get("hint", "")))
        elif kind == "slider_decl":
            for name, index, scope in _slider_declarations(stripped, names):
                if scope == rule.get("scope", "global"):
                    findings.append(LintFinding(rule["id"], _line_at(stripped, index), severity,
                                                rule.get("message", "").replace("{name}", name),
                                                rule.get("hint", "").replace("{name}", name)))
        else:
            raise ValueError(f"unknown lint rule kind: {kind}")
    return sorted(findings, key=lambda f: (f.line, f.rule))


def frame_stats(lumas: Iterable[float]) -> tuple[float, float]:
    """Mean and population standard deviation of a frame's luminances."""
    values = [float(v) for v in lumas]
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, variance ** 0.5


STILL_SHARE = 0.002        # under 0.2% of pixels changed between every pair of frames: still


def motion(frames: Sequence[dict], late: Sequence[dict] = ()) -> dict:
    """`moving`, `still` or `unknown` from each frame's `moved` share (changed pixels against the
    previous frame). `luma_mean` cannot answer this: drifting clouds keep it constant.

    `late` holds frames rendered only for this check ({t, moved} against the first frame). When the
    contact frames look alike but a late frame differs, the shader moves slowly: `moving` with
    `slow: true`, so a slow drift is not mistaken for a shader that ignores iTime."""
    moved = [frame["moved"] for frame in frames[1:] if isinstance(frame.get("moved"), (int, float))]
    late = [{"t": f["t"], "moved": f["moved"]} for f in late if isinstance(f.get("moved"), (int, float))]
    if not moved:
        return {"verdict": "unknown", "moved": [], "max": None, "late": late, "slow": False}
    peak = max(moved)
    slow = peak < STILL_SHARE and any(f["moved"] >= STILL_SHARE for f in late)
    verdict = "moving" if peak >= STILL_SHARE or slow else "still"
    return {"verdict": verdict, "moved": moved, "max": peak, "late": late, "slow": slow}


def flat(frames: Sequence[dict]) -> bool:
    """True when every frame is a single colour (`luma_std < 0.01`) or black (`mean < 0.02`)."""
    if not frames:
        return False
    for frame in frames:
        std = frame.get("luma_std")
        mean = frame.get("luma_mean")
        if std is None or mean is None:
            continue
        if std >= 0.01 and mean >= 0.02:
            return False
    return True


def flash_verdict(lumas: Sequence[float], red_shares: Sequence[float] | None = None,
                  fps: float = 60.0) -> dict:
    """WCAG 2.3.1 general-flash approximation over one second of frames.

    A flash is a pair of opposing transitions, each at least ``0.10`` in relative luminance, where
    the darker of the two states is below ``0.80``. More than three flashes in any one-second
    window is ``block``, exactly three is ``warn``, fewer is ``none``. A saturated-red share that
    moves by ``0.20`` or more is a red flash and never returns ``none``.
    """
    values = [float(v) for v in lumas]
    significant: list[int] = []
    for i in range(len(values) - 1):
        delta = values[i + 1] - values[i]
        if abs(delta) >= _FLASH_STEP and min(values[i], values[i + 1]) < _FLASH_DARK:
            significant.append(i)

    # One flash is a down-then-up (or up-then-down) pair. Transitions pair up, they do not
    # overlap: a 5 Hz square wave is five flashes per second, not nine.
    pairs: list[int] = []
    pending = 0
    for index in significant:
        direction = 1 if values[index + 1] > values[index] else -1
        if pending and direction != pending:
            pairs.append(index)
            pending = 0
        else:
            pending = direction

    window = max(1, int(round(fps * _WINDOW)))
    busiest = 0
    for start in pairs:
        count = sum(1 for index in pairs if start <= index < start + window)
        busiest = max(busiest, count)

    red = False
    if red_shares is not None and len(red_shares) > 1:
        red = max(abs(red_shares[i + 1] - red_shares[i]) for i in range(len(red_shares) - 1)) >= _RED_STEP

    if busiest > 3 or red:
        verdict = "block" if busiest > 3 else "warn"
    elif busiest == 3:
        verdict = "warn"
    else:
        verdict = "none"
    return {"max_general_per_s": busiest, "red": red, "verdict": verdict,
            "pairs": len(pairs), "frames": len(values), "window_frames": window}
