"""The text three r187's TSL transpiler reads: only what the GLSL compiler compiles, token for token.

The transpiler's encoder copies what it reads into a JavaScript module without escaping, and its
preprocessor differs from GLSL's: it does not define `GL_ES`, and it expands a function-like macro
only when the call sits on one line. Each difference lets text the GLSL compiler skipped or dropped
reach the module as JavaScript (each case is a test in `tests/test_exporters.py`).

So the transpiler reads no comments and no directives at all: `prepare` splices line continuations
and removes comments as GLSL ES 3.00 does, refuses any character GLSL code cannot hold, and expands
`#define` macros itself. The harness then compiles this exact text as GLSL before it transpiles.
With no directive left, the GLSL preprocessor has nothing to do, so the compiler checks every
token the encoder will see. If this expansion ever differs from GLSL's, the proof shows `differs`;
it cannot make the encoder read text the compiler did not accept.
"""

from __future__ import annotations

import re


class Refused(ValueError):
    """The shader cannot become TSL input; the message says why and what to change."""


# Every character GLSL ES 3.00 allows outside a comment. GLSL has no strings, and the encoder
# copies a quoted token into the module as it is.
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                        "_ \t\n.,;:+-*/%<>=!&|^~?()[]{}#")
# A C preprocessing number (`1e-5`, `0x1F`, `2.0`), so `e5` inside one is never a macro name.
_TOKEN_RE = re.compile(r"\.?[0-9](?:[eE][+-]|[0-9A-Za-z_.])*|[A-Za-z_]\w*|\n|[ \t]+|.")
_DIRECTIVE_RE = re.compile(r"^[ \t]*#[ \t]*(\w*)(.*)$")
_DEFINE_RE = re.compile(r"([A-Za-z_]\w*)(?:\(([^)]*)\))?(.*)$")
# GLSL's own macros, so `GL_ES` and `__VERSION__` mean in the module what they mean on the GPU.
PREDEFINED = {"GL_ES": "1", "__VERSION__": "300", "GL_FRAGMENT_PRECISION_HIGH": "1"}


def _splice_continuations(text: str) -> str:
    """Join each line that ends in a backslash with the next, as GLSL ES 3.00 does before it
    looks for comments; a blank line follows the joined one, so later line numbers hold."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        line, joined = lines[i], 0
        while line.endswith("\\") and i + 1 < len(lines):
            i += 1
            line, joined = line[:-1] + lines[i], joined + 1
        out.extend([line] + [""] * joined)
        i += 1
    return "\n".join(out)


def strip_comments(source: str) -> str:
    """The source as GLSL reads it before preprocessing: continuations spliced, each comment one
    space that keeps its newlines, so line numbers still match the source."""
    text = _splice_continuations(source.replace("\r\n", "\n").replace("\r", "\n"))
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            out.append(" " + "\n" * text.count("\n", i, stop))
            i = stop
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


class _Macro:
    def __init__(self, params: list[str] | None, body: str):
        self.params = params
        self.body = _TOKEN_RE.findall(body.strip())


def _is_name(token: str) -> bool:
    return token[:1].isalpha() or token[:1] == "_"


def _call_args(tokens: list[str], start: int, line: int) -> tuple[list[list[str]], int]:
    """The argument token lists of a call whose `(` is tokens[start], and the index after `)`."""
    args, current, depth, i = [], [], 0, start
    while i < len(tokens):
        token = tokens[i]
        if token == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif token == ")":
            depth -= 1
            if depth == 0:
                args.append(current)
                return args, i + 1
        elif token == "," and depth == 1:
            args.append(current)
            current = []
            i += 1
            continue
        current.append(token)
        i += 1
    raise Refused(f"a macro call before line {line} is not closed before the next `#` line or the end")


MAX_TOKENS = 200_000


def _expand(tokens: list[str], macros: dict[str, _Macro], disabled: frozenset, line: int) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if len(out) > MAX_TOKENS:
            raise Refused(f"the macros expand to more than {MAX_TOKENS} tokens")
        token = tokens[i]
        macro = macros.get(token) if _is_name(token) and token not in disabled else None
        if macro is None:
            out.append(token)
            i += 1
            continue
        if macro.params is None:
            out += _expand(macro.body, macros, disabled | {token}, line)
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].isspace():
            j += 1
        if j == len(tokens) or tokens[j] != "(":
            out.append(token)
            i += 1
            continue
        args, after = _call_args(tokens, j, line)
        if args == [[]] and not macro.params:
            args = []
        if len(args) != len(macro.params):
            raise Refused(f"`{token}` on line {line} takes {len(macro.params)} arguments, "
                          f"not {len(args)}")
        values = {name: _expand(arg, macros, disabled, line) for name, arg in zip(macro.params, args)}
        body = [part for t in macro.body for part in values.get(t, [t])]
        out += _expand(body, macros, disabled | {token}, line)
        # A call over several lines keeps its newlines after it, so later line numbers hold.
        out += ["\n"] * sum(1 for t in tokens[i:after] if t == "\n")
        i = after
    if len(out) > MAX_TOKENS:
        raise Refused(f"the macros expand to more than {MAX_TOKENS} tokens")
    return out


def prepare(source: str) -> str:
    """The transpiler's input for `source`, or Refused with the reason."""
    code = strip_comments(source)
    for index, char in enumerate(code):
        if char not in _CODE_CHARS:
            shown = repr(char) if char.isprintable() else f"U+{ord(char):04X}"
            raise Refused(f"{shown} on line {code.count(chr(10), 0, index) + 1} is not GLSL "
                          "outside a comment; the TSL transpiler would copy it into the "
                          "JavaScript module, so remove it")
    # The code between two `#` lines is expanded with the macros defined so far; a call may span
    # lines but not a `#` line. Each `#` line becomes an empty line.
    macros = {name: _Macro(None, value) for name, value in PREDEFINED.items()}
    out: list[str] = []
    pending: list[str] = []
    lines = code.split("\n")

    def flush(line: int) -> None:
        try:
            out.extend(_expand(pending, macros, frozenset(), line))
        except RecursionError:
            raise Refused(f"the macros before line {line} nest too deeply") from None
        pending.clear()

    for number, text in enumerate(lines, start=1):
        directive = _DIRECTIVE_RE.match(text)
        if directive is None:
            pending.extend(_TOKEN_RE.findall(text))
        else:
            name, rest = directive.group(1), directive.group(2).strip()
            if name not in ("define", "undef"):
                raise Refused(f"`#{name}` on line {number}: the TSL export takes `#define` and "
                              "`#undef` only (the transpiler decides `#if` blocks differently from "
                              "the GLSL compiler), so keep the branch you use and remove the rest")
            flush(number)
            if name == "undef":
                macros.pop(rest, None)
            else:
                parsed = _DEFINE_RE.match(rest)
                if parsed is None:
                    raise Refused(f"`#define` on line {number} names no macro")
                params = parsed.group(2)
                macros[parsed.group(1)] = _Macro(
                    None if params is None else [p.strip() for p in params.split(",") if p.strip()],
                    parsed.group(3))
        if number < len(lines):
            pending.append("\n")
    flush(len(lines))
    return "".join(out)
