"""The library's pure logic (F35, F37): rows and states, filters, sort and paging, version
summaries, compare, and the licence rules (constraint 8).

Everything takes records and meta dicts and returns the shapes of `experiments/.../v02-contract.md`
(camelCase keys, because Desktop reads them as they are). No database, no Hermes, no files except
the thumbnails a caller passes in as data URLs.
"""

from __future__ import annotations

import difflib
import json
import os
import re

from . import record

STATES = ("ok", "flat", "flash_warn", "flash_block", "no_thumb", "frames_pruned", "legacy")
SORTS = ("updated", "created", "name")
MAX_LIMIT = 100
TAG_LIMIT = 8
TAG_LENGTH = 24
NAME_LIMIT = 80
DESCRIPTION_LIMIT = 2000


# ---------------------------------------------------------------- states

def flash_verdict(doc: dict) -> str | None:
    verdict = ((doc.get("checks") or {}).get("flash") or {}).get("verdict")
    return verdict if verdict in ("none", "warn", "block") else None


def states(doc: dict, *, has_thumb: bool) -> list[str]:
    """What the page shows about a version besides `ok` (an empty list reads as ok)."""
    checks = doc.get("checks") or {}
    out = []
    if checks.get("flat") is True:
        out.append("flat")
    verdict = flash_verdict(doc)
    if verdict == "warn":
        out.append("flash_warn")
    elif verdict == "block":
        out.append("flash_block")
    elif verdict is None:
        out.append("legacy")
    if not has_thumb:
        out.append("no_thumb")
    sheet = (doc.get("files") or {}).get("contact_sheet")
    if sheet and not _exists(sheet):
        out.append("frames_pruned")
    return out


def autoplay(doc: dict) -> bool:
    """Constraint 7: only a server-checked version with flash verdict `none` plays by itself."""
    return (doc.get("checks") or {}).get("status") == "ok" and flash_verdict(doc) == "none"


def _exists(path: str) -> bool:
    return os.path.isfile(path)


# ---------------------------------------------------------------- rows

def row(latest: dict, meta: dict, *, versions: int, thumb: str | None, created: str = "",
        parent: dict | None = None, has_thumb: bool | None = None, thumb_stamp: int | None = None) -> dict:
    """The contract's `LibraryRow` for one shader. `thumbVersion` names the version whose thumb
    exists (the page fetches it through GET /thumbs) and `thumbStamp` when that file was written,
    so a thumb re-rendered after slider tuning is fetched again; `thumb` is filled only where one
    shader is answered (detail, PATCH, fork)."""
    has_thumb = thumb is not None if has_thumb is None else has_thumb
    provenance = latest.get("provenance") or {}
    imported = None
    if provenance.get("origin") == "import":
        imported = {"url": provenance.get("url"), "author": provenance.get("author")}
    updated = max(str(meta.get("updated_at") or ""), str(latest.get("created_at") or ""))
    return {
        "id": latest["id"],
        "name": record.display_name(meta.get("name") or latest.get("name") or latest["id"]),
        "version": int(latest["version"]),
        "versions": int(versions),
        "states": states(latest, has_thumb=has_thumb),
        "autoplay": autoplay(latest),
        "flash": flash_verdict(latest),
        "licence": meta.get("licence"),
        "licenceInherited": meta.get("licence_inherited"),
        "parent": parent,
        "imported": imported,
        "starred": bool(meta.get("starred")),
        "archived": bool(meta.get("archived_at")),
        "tags": list(meta.get("tags") or []),
        "updatedAt": updated,
        "createdAt": created or str(latest.get("created_at") or ""),
        "authorKind": record.author_kind(latest),
        "thumb": thumb,
        "thumbVersion": int(latest["version"]) if has_thumb else None,
        "thumbStamp": thumb_stamp if has_thumb else None,
    }


def version_summary(doc: dict, *, thumb: str | None = None, thumb_stamp: int | None = None) -> dict:
    """One version in the filmstrip or a compare. The filmstrip passes only `thumb_stamp` and
    fetches the images through GET /thumbs; a compare inlines its two."""
    provenance = doc.get("provenance") or {}
    return {"version": int(doc["version"]), "authorKind": record.author_kind(doc),
            "states": states(doc, has_thumb=thumb is not None or thumb_stamp is not None), "thumb": thumb,
            "thumbStamp": thumb_stamp, "autoplay": autoplay(doc), "flash": flash_verdict(doc),
            "createdAt": doc.get("created_at") or "", "licence": provenance.get("licence"),
            "parentVersion": provenance.get("parent_version")}


def version_doc(doc: dict, meta: dict, *, thumb: str | None) -> dict:
    """The contract's `VersionDoc`: everything a canvas, the sliders and the Lab need."""
    checks = doc.get("checks") or {}
    return {
        "id": doc["id"], "version": int(doc["version"]),
        "name": record.display_name(meta.get("name") or doc.get("name") or doc["id"]),
        "source": doc.get("source") or "", "sliders": doc.get("sliders") or [],
        "tunedValues": record.tuned_values(doc), "effectiveValues": record.effective_values(doc),
        "checks": {"status": checks.get("status") or "unknown", "flash": flash_verdict(doc),
                   "motion": (checks.get("motion") or {}).get("verdict"), "flat": checks.get("flat") is True},
        "states": states(doc, has_thumb=thumb is not None), "autoplay": autoplay(doc),
        "authorKind": record.author_kind(doc), "createdAt": doc.get("created_at") or "",
        "licence": (doc.get("provenance") or {}).get("licence"), "thumb": thumb,
    }


# ---------------------------------------------------------------- filters, sort, paging

def parse_query(params: dict) -> dict:
    """The page's query string, validated. Unknown values fall back to the defaults."""
    def flag(name):
        return str(params.get(name) or "").lower() in {"1", "true", "yes"}

    sort = str(params.get("sort") or "updated")
    try:
        limit = max(1, min(MAX_LIMIT, int(params.get("limit") or 48)))
    except (TypeError, ValueError):
        limit = 48
    try:
        offset = max(0, int(params.get("cursor") or 0))
    except (TypeError, ValueError):
        offset = 0
    state = str(params.get("state") or "")
    return {"q": str(params.get("q") or "").strip().lower()[:80],
            "state": state if state in STATES else "",
            "licence": str(params.get("licence") or "").strip(),
            "tag": str(params.get("tag") or "").strip().lower()[:TAG_LENGTH],
            "starred": flag("starred"), "archived": flag("archived"),
            "sort": sort if sort in SORTS else "updated", "limit": limit, "offset": offset}


def matches(item: dict, query: dict) -> bool:
    if item["archived"] != query["archived"]:
        return False
    if query["starred"] and not item["starred"]:
        return False
    if query["q"]:
        haystack = " ".join([item["name"].lower(), item["id"], *item["tags"]])
        if query["q"] not in haystack:
            return False
    if query["state"]:
        if query["state"] == "ok" and item["states"]:
            return False
        if query["state"] != "ok" and query["state"] not in item["states"]:
            return False
    if query["licence"]:
        wanted = None if query["licence"].lower() == "private" else query["licence"]
        if item["licence"] != wanted:
            return False
    if query["tag"] and query["tag"] not in item["tags"]:
        return False
    return True


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "name":
        ordered = sorted(rows, key=lambda r: (r["name"].lower(), r["id"]))
    elif sort == "created":
        ordered = sorted(rows, key=lambda r: (r["createdAt"], r["id"]), reverse=True)
    else:
        ordered = sorted(rows, key=lambda r: (r["updatedAt"], r["id"]), reverse=True)
    return sorted(ordered, key=lambda r: not r["starred"])   # stable: starred first in every sort


def page(rows: list[dict], query: dict) -> tuple[list[dict], int, str | None]:
    selected = sort_rows([r for r in rows if matches(r, query)], query["sort"])
    window = selected[query["offset"]:query["offset"] + query["limit"]]
    end = query["offset"] + len(window)
    return window, len(selected), (str(end) if end < len(selected) else None)


def pick_up(rows: list[dict]) -> dict | None:
    live = [r for r in rows if not r["archived"]]
    return max(live, key=lambda r: (r["updatedAt"], r["id"])) if live else None


def facets(rows: list[dict]) -> dict:
    return {"tags": sorted({tag for r in rows for tag in r["tags"]}),
            "licences": sorted({r["licence"] or "private" for r in rows})}


# ---------------------------------------------------------------- meta edits

def clean_tags(tags) -> list[str] | str:
    """Lowercase, trimmed, unique tags (max 8, max 24 chars each), or an error sentence."""
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return "tags must be a list of strings"
    out: list[str] = []
    for tag in tags:
        clean = re.sub(r"\s+", "-", tag.strip().lower())
        if not clean:
            continue
        if len(clean) > TAG_LENGTH:
            return f"tag {clean!r} is longer than {TAG_LENGTH} characters"
        if clean not in out:
            out.append(clean)
    if len(out) > TAG_LIMIT:
        return f"at most {TAG_LIMIT} tags"
    return out


def clean_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    text = " ".join(name.split())[:NAME_LIMIT]
    return text or None


# ---------------------------------------------------------------- compare

def compare(a: dict, b: dict) -> dict:
    """Line diff of the sources and the slider changes between two versions."""
    a_lines = (a.get("source") or "").splitlines()
    b_lines = (b.get("source") or "").splitlines()
    lines = []
    matcher = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                lines.append({"op": " ", "a": i1 + offset + 1, "b": j1 + offset + 1, "text": a_lines[i1 + offset]})
            continue
        for index in range(i1, i2):
            lines.append({"op": "-", "a": index + 1, "b": None, "text": a_lines[index]})
        for index in range(j1, j2):
            lines.append({"op": "+", "a": None, "b": index + 1, "text": b_lines[index]})
    a_sliders = {s["name"]: s for s in a.get("sliders") or []}
    b_sliders = {s["name"]: s for s in b.get("sliders") or []}
    sliders = []
    for name in list(a_sliders) + [n for n in b_sliders if n not in a_sliders]:
        left, right = a_sliders.get(name), b_sliders.get(name)
        shape = lambda s: {"min": s["min"], "max": s["max"], "default": s["default"]} if s else None  # noqa: E731
        if left and right:
            change = "same" if shape(left) == shape(right) else "changed"
        else:
            change = "removed" if left else "added"
        sliders.append({"name": name, "change": change, "a": shape(left), "b": shape(right)})
    return {"lines": lines, "sliders": sliders}


# ---------------------------------------------------------------- licences (constraint 8)

LICENCES = (
    (None, "Private", "Only you have it. Nobody else may use it; this is the default."),
    ("MIT", "MIT", "Anyone may use, change and sell it, if they keep your name and the licence text."),
    ("CC0-1.0", "CC0", "Public domain: anyone may do anything with it, no credit needed."),
    ("CC-BY-4.0", "CC BY 4.0", "Anyone may use and change it, also commercially, if they credit you."),
    ("CC-BY-SA-4.0", "CC BY-SA 4.0", "Like CC BY, and changed versions must carry the same licence."),
    ("CC-BY-NC-4.0", "CC BY-NC 4.0", "Anyone may use and change it with credit, but not commercially."),
    ("CC-BY-NC-SA-4.0", "CC BY-NC-SA 4.0",
     "Non-commercial use with credit, and changed versions carry the same licence."),
    ("CC-BY-NC-SA-3.0", "CC BY-NC-SA 3.0",
     "Shadertoy's default: non-commercial use with credit, changes under the same licence."),
)
_CC_RE = re.compile(r"^CC-(?P<elements>(?:BY|NC|SA|ND)(?:-(?:BY|NC|SA|ND))*)-(?P<version>\d+\.\d+)$")


def _cc_parts(licence: str | None) -> tuple[tuple[str, ...], float] | None:
    match = _CC_RE.match(str(licence or ""))
    if not match:
        return None
    return tuple(match.group("elements").split("-")), float(match.group("version"))


def inherits(licence: str | None) -> bool:
    """A claimed licence that binds every copy: non-commercial or share-alike."""
    parts = _cc_parts(licence)
    return bool(parts and ({"NC", "SA"} & set(parts[0])))


def no_derivatives(licence: str | None) -> bool:
    """CC ND: copies only as they are. Every import is changed (normalised, forked, edited)."""
    parts = _cc_parts(licence)
    return bool(parts and "ND" in parts[0])


ND_REFUSAL = ("{licence} is a no-derivatives (ND) licence: it allows copies only as they are, and "
              "Shaderbrücke changes every shader it imports (it normalises, forks and edits them)")


def known(licence: str | None) -> bool:
    return licence is None or any(spdx == licence for spdx, _, _ in LICENCES) or _cc_parts(licence) is not None


def licence_change_refusal(meta: dict, licence: str | None) -> str | None:
    """None when `licence` may be picked; otherwise the reason. An inherited licence allows only
    itself or a later version with the same elements (BY-NC-SA 3.0 -> 4.0)."""
    if not known(licence):
        return f"{licence} is not a licence the picker knows"
    inherited = meta.get("licence_inherited")
    if not inherited:
        return None
    if licence == inherited:
        return None
    own, wanted = _cc_parts(inherited), _cc_parts(licence)
    if own and wanted and own[0] == wanted[0] and wanted[1] >= own[1]:
        return None
    return (f"this shader comes from a work licensed {inherited}; its copies keep that licence "
            "(or a later version of the same licence)")


def licence_options(meta: dict) -> list[dict]:
    """The picker's rows with `allowed` and the reason when not."""
    rows = []
    for spdx, label, meaning in LICENCES:
        refusal = licence_change_refusal(meta, spdx)
        rows.append({"id": spdx, "label": label, "meaning": meaning, "allowed": refusal is None,
                     "reason": refusal})
    inherited = meta.get("licence_inherited")
    if inherited and all(spdx != inherited for spdx, _, _ in LICENCES):
        rows.append({"id": inherited, "label": inherited, "meaning": "The licence of the original work.",
                     "allowed": True, "reason": None})
    return rows


# ---------------------------------------------------------------- share files (F41, format shaderbruecke/1)

SHARE_FORMAT = "shaderbruecke/1"
SHARE_SUFFIX = ".shaderbruecke.json"
SHARE_MAX_BYTES = 1024 * 1024
SOURCE_MAX_BYTES = 64 * 1024
CLAIM_TEXT = 200
CLAIM_ITEMS = 20


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40] or "shader"


def ai_involvement(kinds: list[str]) -> str:
    """`generated` when the agent wrote every version, `assisted` for a mix, `none` when only the
    user did; an import keeps the claim it came with (`unknown` without one)."""
    own = [kind for kind in kinds if kind in ("agent", "human")]
    if not own:
        return "unknown"
    if all(kind == "agent" for kind in own):
        return "generated"
    return "none" if all(kind == "human" for kind in own) else "assisted"


def build_share(doc: dict, meta: dict, *, kinds: list[str], settings: dict, thumb: str | None,
                parent: dict | None, made_with: str) -> dict:
    """The share file of one version. Everything another user's importer shows as a claim
    (author, licence, lineage, third party) comes from the record and the settings."""
    provenance = doc.get("provenance") or {}
    lineage = [dict(item) for item in provenance.get("lineage") or [] if isinstance(item, dict)]
    if provenance.get("origin") == "import" and (provenance.get("author") or provenance.get("url")):
        lineage.append({"name": provenance.get("imported_name") or meta.get("name"),
                        "author": provenance.get("author"), "licence": provenance.get("licence"),
                        "url": provenance.get("url")})
    if parent:
        lineage.append({"name": parent.get("name"), "author": settings.get("author_name") or None,
                        "licence": parent.get("licence"), "url": None})
    involvement = provenance.get("ai_involvement") if provenance.get("origin") == "import" else None
    return {
        "format": SHARE_FORMAT,
        "name": record.display_name(meta.get("name") or doc.get("name") or "shader"),
        "source": doc.get("source") or "",
        "sliders": [{key: slider.get(key) for key in ("name", "min", "max", "default", "step", "label")
                     if key in slider} for slider in doc.get("sliders") or []],
        "tuned_values": record.tuned_values(doc),
        "licence": provenance.get("licence"),
        "licence_inherited": meta.get("licence_inherited"),
        "author": {"name": settings.get("author_name") or None, "url": settings.get("author_url") or None},
        "ai_involvement": involvement or ai_involvement(kinds),
        "third_party": [dict(item) for item in provenance.get("third_party") or [] if isinstance(item, dict)],
        "lineage": lineage[:CLAIM_ITEMS],
        "tags": list(meta.get("tags") or []),
        "description": str(meta.get("description") or "")[:DESCRIPTION_LIMIT],
        "thumb": thumb,
        "made_with": made_with,
    }


def _claim_text(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())[:CLAIM_TEXT]
    return text or None


def _claim_url(value) -> str | None:
    """A claimed link, kept only when it is an http(s) URL."""
    text = _claim_text(value)
    return text if text and text.startswith(("https://", "http://")) and " " not in text else None


def _claim_items(items) -> list[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            out.append({key: (_claim_url if key == "url" else _claim_text)(item.get(key))
                        for key in ("name", "what", "author", "licence", "url") if key in item})
        if len(out) >= CLAIM_ITEMS:
            break
    return out


def parse_share(data) -> tuple[dict | None, str | None]:
    """A share file as the importer may use it, or the reason it is refused. Only the claims
    (name, author, licence, lineage, third party, tags, description, tuned values) are kept; the
    sliders and the thumbnail are rebuilt from the source by the importer."""
    if not isinstance(data, dict):
        return None, "a share file is a JSON object"
    if data.get("format") != SHARE_FORMAT:
        return None, f"unknown format {data.get('format')!r}; this importer reads {SHARE_FORMAT}"
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        return None, "the share file has no source"
    if len(source.encode("utf-8")) > SOURCE_MAX_BYTES:
        return None, f"the source is larger than {SOURCE_MAX_BYTES} bytes"
    licence = data.get("licence")
    if licence is not None and (not isinstance(licence, str) or not known(licence)):
        return None, f"unknown licence {licence!r}"
    for claimed in (licence, data.get("licence_inherited")):
        if isinstance(claimed, str) and no_derivatives(claimed):
            return None, ND_REFUSAL.format(licence=claimed)
    tuned = data.get("tuned_values") if isinstance(data.get("tuned_values"), dict) else {}
    tags = clean_tags(data.get("tags") or [])
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    return {
        "name": clean_name(data.get("name")) or "imported shader",
        "source": source,
        "tuned_values": {str(k): v for k, v in tuned.items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
        "licence": licence,
        "licence_inherited": data.get("licence_inherited") if known(data.get("licence_inherited")) else None,
        "author": _claim_text(author.get("name")),
        "author_url": _claim_url(author.get("url")),
        "ai_involvement": data.get("ai_involvement") if data.get("ai_involvement") in
        ("none", "assisted", "generated", "unknown") else "unknown",
        "third_party": _claim_items(data.get("third_party")),
        "lineage": _claim_items(data.get("lineage")),
        "tags": tags if isinstance(tags, list) else [],
        "description": str(data.get("description") or "")[:DESCRIPTION_LIMIT],
    }, None


# ---------------------------------------------------------------- pasted shaders (Shadertoy, glslCanvas, ISF)

SHADERTOY_DEFAULT_LICENCE = "CC-BY-NC-SA-3.0"
_ISF_HEADER_RE = re.compile(r"^\s*/\*\s*(\{.*?\})\s*\*/", re.S)
_MAIN_RE = re.compile(r"\bvoid\s+main\s*\(\s*(?:void)?\s*\)")
_PREAMBLE_RE = re.compile(r"^\s*(#version[^\n]*|precision\s+\w+\s+\w+\s*;|#ifdef\s+GL_ES|#endif)\s*$", re.M)


def detect_dialect(source: str) -> str:
    if _ISF_HEADER_RE.match(source or "") and ("ISFVSN" in source or '"INPUTS"' in source):
        return "isf"
    if re.search(r"\bmainImage\s*\(", source or ""):
        return "shadertoy"
    if _MAIN_RE.search(source or "") and re.search(r"\bu_(time|resolution|mouse)\b|gl_FragColor", source or ""):
        return "glslcanvas"
    return "unknown"


def _main_to_main_image(body: str) -> str:
    body = _MAIN_RE.sub("void mainImage(out vec4 fragColor, in vec2 fragCoord)", body, count=1)
    body = re.sub(r"\bgl_FragColor\b", "fragColor", body)
    body = re.sub(r"\bgl_FragCoord\s*\.\s*xy\b", "fragCoord", body)
    return re.sub(r"\bgl_FragCoord\b", "vec4(fragCoord, 0.5, 1.0)", body)


def _isf_input(name: str, kind, item: dict) -> tuple[str | None, str | None]:
    """One ISF input as a `@slider` line or a constant, and the warning for a constant."""
    if kind == "float":
        low, high = float(item.get("MIN", 0.0)), float(item.get("MAX", 1.0))
        default = min(high, max(low, float(item.get("DEFAULT", low))))
        label = " ".join(str(item.get("LABEL") or "").split()).replace('"', "")[:60]
        return f"// @slider {name} {low:g}..{high:g} = {default:g}" + (f' "{label}"' if label else ""), None
    if kind == "bool":
        return (f"const bool {name} = {'true' if item.get('DEFAULT') else 'false'};",
                f"the ISF input {name} (bool) became a constant")
    if kind in ("long", "event"):
        return f"const int {name} = {int(item.get('DEFAULT') or 0)};", f"the ISF input {name} ({kind}) became a constant"
    if kind == "color":
        value = list(item.get("DEFAULT") or [1, 1, 1, 1])[:4] + [1.0] * 4
        return (f"const vec4 {name} = vec4({', '.join(f'{float(v):g}' for v in value[:4])});",
                f"the ISF input {name} (color) became a constant")
    if kind == "point2D":
        value = list(item.get("DEFAULT") or [0, 0])[:2] + [0.0, 0.0]
        return (f"const vec2 {name} = vec2({float(value[0]):g}, {float(value[1]):g});",
                f"the ISF input {name} (point2D) became a constant")
    return None, None


def _isf(source: str) -> tuple[str | None, list[str], str | None, dict]:
    match = _ISF_HEADER_RE.match(source)
    try:
        header = json.loads(match.group(1))
    except (ValueError, AttributeError):
        return None, [], "the ISF header is not valid JSON", {}
    if not isinstance(header, dict):
        return None, [], "the ISF header is not a JSON object", {}
    if header.get("PASSES") or header.get("PERSISTENT_BUFFERS"):
        return None, [], "multipass ISF shaders (PASSES) are not supported yet", {}
    inputs = header.get("INPUTS") or []
    if not isinstance(inputs, list):
        return None, [], "the ISF header's INPUTS is not a list", {}
    lines, warnings = [], []
    for item in inputs:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z_]\w*", str(item.get("NAME") or "")):
            continue
        name, kind = item["NAME"], item.get("TYPE")
        if kind in ("image", "audio", "audioFFT"):
            return None, [], f"the ISF input {name} is {kind}; image and audio inputs are not supported yet", {}
        try:
            line, warning = _isf_input(name, kind, item)
        except (TypeError, ValueError):
            return None, [], f"the ISF input {name} has a MIN, MAX or DEFAULT that is not a number", {}
        if line:
            lines.append(line)
        if warning:
            warnings.append(warning)
    body = source[match.end():]
    if re.search(r"\bIMG_(NORM_)?PIXEL|IMG_THIS|IMG_SIZE\b", body):
        return None, [], "the shader samples images (IMG_PIXEL); image inputs are not supported yet", {}
    for isf_name, ours in (("isf_FragNormCoord", "(fragCoord / iResolution.xy)"), ("RENDERSIZE", "iResolution.xy"),
                           ("TIMEDELTA", "iTimeDelta"), ("FRAMEINDEX", "iFrame"), ("DATE", "iDate"),
                           ("TIME", "iTime")):
        body = re.sub(rf"\b{isf_name}\b", ours, body)
    body = _PREAMBLE_RE.sub("", body)
    info = {"name": header.get("DESCRIPTION") and str(header["DESCRIPTION"])[:80],
            "author": header.get("CREDIT")}
    return "\n".join(lines) + ("\n" if lines else "") + _main_to_main_image(body).strip() + "\n", warnings, None, info


def _glslcanvas(source: str) -> tuple[str | None, list[str], str | None]:
    if re.search(r"\btexture2D\b|\bsampler2D\b", source):
        return None, [], "the shader samples textures (sampler2D); texture inputs are not supported yet"
    body = _PREAMBLE_RE.sub("", source)
    body = re.sub(r"^\s*uniform\s+\w+\s+u_(time|resolution|mouse)\s*;\s*$", "", body, flags=re.M)
    body = re.sub(r"\bu_time\b", "iTime", body)
    body = re.sub(r"\bu_resolution\b", "iResolution.xy", body)
    body = re.sub(r"\bu_mouse\b", "iMouse.xy", body)
    leftover = re.findall(r"^\s*uniform\s+\w+\s+(\w+)\s*;", body, flags=re.M)
    if leftover:
        return None, [], f"the shader needs uniforms the host does not set: {', '.join(leftover)}"
    return _main_to_main_image(body).strip() + "\n", [], None


def normalise_pasted(source: str) -> dict:
    """`{dialect, source, warnings, refused, name, author}` for a pasted shader. Multipass, buffers,
    textures and audio are refused with the reason; nothing is fetched."""
    dialect = detect_dialect(source)
    if dialect == "shadertoy":
        if re.search(r"\biChannel[0-3]\b|\biChannelResolution\b", source):
            return {"dialect": dialect, "source": None, "warnings": [], "name": None, "author": None,
                    "refused": "the shader reads iChannel inputs (textures, buffers or audio); "
                               "only single-pass Image shaders without inputs are supported yet"}
        cleaned = _PREAMBLE_RE.sub("", source).strip() + "\n"
        return {"dialect": dialect, "source": cleaned, "warnings": [], "refused": None, "name": None, "author": None}
    if dialect == "isf":
        body, warnings, refused, info = _isf(source)
        return {"dialect": dialect, "source": body, "warnings": warnings, "refused": refused,
                "name": info.get("name"), "author": info.get("author")}
    if dialect == "glslcanvas":
        body, warnings, refused = _glslcanvas(source)
        return {"dialect": dialect, "source": body, "warnings": warnings, "refused": refused, "name": None,
                "author": None}
    return {"dialect": dialect, "source": None, "warnings": [], "name": None, "author": None,
            "refused": "not a Shadertoy (mainImage), glslCanvas (u_time, gl_FragColor) or ISF shader"}
