"""Compile and snapshot a Shadertoy-style GLSL shader in a short-lived headless Chromium.

The browser is Hermes' own packaged Chromium, spawned as a child process with a throwaway
profile and no network (every host resolves to NOTFOUND). It is driven over the DevTools
pipe (`--remote-debugging-pipe`: JSON messages on fds 3/4, NUL-terminated). One hard
wall-clock deadline covers launch, compile, draw and readback; when it passes, the whole
process group is killed. Shader code never leaves this machine.

`render()` compiles a shader on `about:blank` with the header/footer below and returns one
frame. `render_bundle()` renders the three contact-sheet frames, a NaN pass and a one-second
flash scan in the same browser session. `run_page()` opens a page from a local folder and
evaluates an expression there, which is how the offline three.js proof harness
(`headless/harness.html`) runs: the browser asks this process for every file over the DevTools
pipe, which answers only from that folder, so the page reads no other local file.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import fcntl
import json
import os
import re
import select
import shutil
import signal
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

# Source string 0 is this header, 1 is the agent's shader, 2 is the footer. The `#line`
# directives make the driver report errors as `1:<line>` in the agent's own numbering.
HEADER = """#version 300 es
precision highp float;
precision highp int;
uniform vec3 iResolution;
uniform float iTime;
uniform float iTimeDelta;
uniform int iFrame;
uniform vec4 iMouse;
uniform vec4 iDate;
out vec4 sb_FragColor;
#line 1 1
"""

FOOTER = """
#line 1 2
void main() {
  vec4 color = vec4(0.0, 0.0, 0.0, 1.0);
  mainImage(color, gl_FragCoord.xy);
  sb_FragColor = color;
}
"""

VERTEX = """#version 300 es
in vec2 p;
void main() { gl_Position = vec4(p, 0.0, 1.0); }
"""

# Evaluated in an about:blank tab; returns a plain object (returnByValue).
_PROBE_JS = """(() => {
const CFG = __CFG__;
function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  return [s, !!gl.getShaderParameter(s, gl.COMPILE_STATUS), gl.getShaderInfoLog(s) || ''];
}
const out = { status: 'ok' };
const W = CFG.width, H = CFG.height, T = CFG.time;
const canvas = document.createElement('canvas');
canvas.width = W; canvas.height = H;
document.body.appendChild(canvas);
const gl = canvas.getContext('webgl2', { preserveDrawingBuffer: true, antialias: false });
if (!gl) { out.status = 'no_webgl2'; return out; }
const dbg = gl.getExtension('WEBGL_debug_renderer_info');
out.renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
const [vs, vsOk, vsLog] = compile(gl, gl.VERTEX_SHADER, CFG.vertex);
if (!vsOk) { out.status = 'internal_error'; out.compile_log = vsLog; return out; }
const [fs, ok, log] = compile(gl, gl.FRAGMENT_SHADER, CFG.header + CFG.source + CFG.footer);
out.compile_ok = ok;
out.compile_log = log;
if (!ok) { out.status = 'compile_error'; return out; }
const prog = gl.createProgram();
gl.attachShader(prog, vs);
gl.attachShader(prog, fs);
gl.bindAttribLocation(prog, 0, 'p');
gl.linkProgram(prog);
out.link_ok = !!gl.getProgramParameter(prog, gl.LINK_STATUS);
out.link_log = gl.getProgramInfoLog(prog) || '';
if (!out.link_ok) { out.status = 'link_error'; return out; }
gl.useProgram(prog);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0);
gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
const u = name => gl.getUniformLocation(prog, name);
gl.uniform3f(u('iResolution'), W, H, 1.0);
gl.uniform1f(u('iTime'), T);
gl.uniform1f(u('iTimeDelta'), 1.0 / 60.0);
gl.uniform1i(u('iFrame'), Math.round(T * 60));
gl.uniform4f(u('iMouse'), 0, 0, 0, 0);
gl.uniform4f(u('iDate'), 2026, 10, 6, 0);
gl.viewport(0, 0, W, H);
const t0 = performance.now();
gl.drawArrays(gl.TRIANGLES, 0, 3);
const px = new Uint8Array(W * H * 4);
gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, px);
out.draw_ms = Math.round((performance.now() - t0) * 10) / 10;
let sum = 0, sum2 = 0;
for (let i = 0; i < px.length; i += 4) {
  const y = (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255;
  sum += y; sum2 += y * y;
}
const n = W * H, mean = sum / n;
out.luma_mean = Math.round(mean * 1000) / 1000;
out.luma_std = Math.round(Math.sqrt(Math.max(0, sum2 / n - mean * mean)) * 1000) / 1000;
out.png = canvas.toDataURL('image/png');
return out;
})()"""

# The render bundle: three frames for the contact sheet, a float pass for a NaN verdict, and a
# one-second scan that only returns numbers (the flash verdict is computed in Python).
# The expression only installs `window.__sbBundle`; Python then calls its steps one by one
# (compile, frames, flash), so a deadline always knows which phase was running.
_BUNDLE_JS = """(() => {
const CFG = __CFG__;
function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  return [s, !!gl.getShaderParameter(s, gl.COMPILE_STATUS), gl.getShaderInfoLog(s) || ''];
}
function build(w, h) {
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  document.body.appendChild(canvas);
  const gl = canvas.getContext('webgl2', { preserveDrawingBuffer: true, antialias: false });
  if (!gl) return null;
  const [vs, vsOk, vsLog] = compile(gl, gl.VERTEX_SHADER, CFG.vertex);
  if (!vsOk) return { error: 'vertex shader: ' + vsLog };
  const [fs, ok, log] = compile(gl, gl.FRAGMENT_SHADER, CFG.header + CFG.source + CFG.footer);
  if (!ok) return { error: log, compile_log: log };
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.bindAttribLocation(prog, 0, 'p');
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    return { error: 'link: ' + (gl.getProgramInfoLog(prog) || ''), compile_log: gl.getProgramInfoLog(prog) || '' };
  }
  gl.useProgram(prog);
  for (const [name, value] of Object.entries(CFG.sliders || {})) {
    const loc = gl.getUniformLocation(prog, name);
    if (loc) gl.uniform1f(loc, value);
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  return { gl, prog, w, h, u: name => gl.getUniformLocation(prog, name) };
}
function setTime(g, t) {
  g.gl.uniform3f(g.u('iResolution'), g.w, g.h, 1.0);
  g.gl.uniform1f(g.u('iTime'), t);
  g.gl.uniform1f(g.u('iTimeDelta'), 1.0 / 60.0);
  g.gl.uniform1i(g.u('iFrame'), Math.round(t * 60));
  g.gl.uniform4f(g.u('iMouse'), 0, 0, 0, 0);
  g.gl.uniform4f(g.u('iDate'), 2026, 10, 6, 0);
  g.gl.viewport(0, 0, g.w, g.h);
}
function stats(px) {
  let sum = 0, sum2 = 0;
  const n = px.length / 4;
  for (let i = 0; i < px.length; i += 4) {
    const y = (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255;
    sum += y; sum2 += y * y;
  }
  const mean = sum / n;
  return [Math.round(mean * 1000) / 1000, Math.round(Math.sqrt(Math.max(0, sum2 / n - mean * mean)) * 1000) / 1000];
}
// Share of pixels whose luminance moved more than 2/255 since the previous frame. Whole-frame
// statistics miss drift: clouds that slide sideways keep the mean and spread unchanged.
function movedShare(a, b) {
  let moved = 0;
  for (let i = 0; i < b.length; i += 4) {
    const ya = 0.2126 * a[i] + 0.7152 * a[i + 1] + 0.0722 * a[i + 2];
    const yb = 0.2126 * b[i] + 0.7152 * b[i + 1] + 0.0722 * b[i + 2];
    if (Math.abs(ya - yb) > 2) moved++;
  }
  return Math.round((moved / (b.length / 4)) * 10000) / 10000;
}
function srgbToLinear(c) {
  c /= 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function relativeLuminance(px) {
  let sum = 0;
  const n = px.length / 4;
  for (let i = 0; i < px.length; i += 4) {
    sum += 0.2126 * srgbToLinear(px[i]) + 0.7152 * srgbToLinear(px[i + 1]) + 0.0722 * srgbToLinear(px[i + 2]);
  }
  return sum / n;
}
function redShare(px) {
  let red = 0;
  const n = px.length / 4;
  for (let i = 0; i < px.length; i += 4) {
    if (px[i] > 96 && px[i] > 2 * px[i + 1] && px[i] > 2 * px[i + 2]) red++;
  }
  return Math.round((red / n) * 1000) / 1000;
}
function toPng(px, w, h) {
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(w, h);
  const stride = w * 4;
  for (let y = 0; y < h; y++) {
    const src = (h - 1 - y) * stride;          // GL reads bottom-up
    image.data.set(px.subarray(src, src + stride), y * stride);
  }
  ctx.putImageData(image, 0, 0);
  return canvas.toDataURL('image/png');
}
function sheet(frames, W, H, columns, cellW) {
  const cellH = Math.round(cellW * H / W);
  const canvas = document.createElement('canvas');
  canvas.width = cellW * columns;
  canvas.height = cellH;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(canvas.width, canvas.height);
  for (let c = 0; c < frames.length && c < columns; c++) {
    const px = frames[c].pixels || frames[c];
    for (let y = 0; y < cellH; y++) {
      const sy = Math.min(H - 1, Math.floor(y * H / cellH));
      for (let x = 0; x < cellW; x++) {
        const sx = Math.min(W - 1, Math.floor(x * W / cellW));
        const src = ((H - 1 - sy) * W + sx) * 4;          // GL reads bottom-up
        const dst = (y * canvas.width + c * cellW + x) * 4;
        image.data[dst] = px[src];
        image.data[dst + 1] = px[src + 1];
        image.data[dst + 2] = px[src + 2];
        image.data[dst + 3] = 255;
      }
    }
  }
  ctx.putImageData(image, 0, 0);
  return canvas.toDataURL('image/png');
}

const out = { status: 'ok', frames: [], times: CFG.times };
const state = {};
window.__sbBundle = {
compile() {
const main = build(CFG.width, CFG.height);
if (main === null) { out.status = 'no_webgl2'; return out; }
if (main.error) { out.status = 'compile_error'; out.compile_log = main.error; return out; }
state.main = main;
const dbg = main.gl.getExtension('WEBGL_debug_renderer_info');
out.renderer = dbg ? main.gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : main.gl.getParameter(main.gl.RENDERER);

// Float target for the NaN pass, when the driver can render to one.
let floatFb = null, floatTex = null;
if (main.gl.getExtension('EXT_color_buffer_float')) {
  floatTex = main.gl.createTexture();
  main.gl.bindTexture(main.gl.TEXTURE_2D, floatTex);
  main.gl.texImage2D(main.gl.TEXTURE_2D, 0, main.gl.RGBA32F, CFG.width, CFG.height, 0, main.gl.RGBA, main.gl.FLOAT, null);
  main.gl.texParameteri(main.gl.TEXTURE_2D, main.gl.TEXTURE_MIN_FILTER, main.gl.NEAREST);
  main.gl.texParameteri(main.gl.TEXTURE_2D, main.gl.TEXTURE_MAG_FILTER, main.gl.NEAREST);
  floatFb = main.gl.createFramebuffer();
  main.gl.bindFramebuffer(main.gl.FRAMEBUFFER, floatFb);
  main.gl.framebufferTexture2D(main.gl.FRAMEBUFFER, main.gl.COLOR_ATTACHMENT0, main.gl.TEXTURE_2D, floatTex, 0);
  if (main.gl.checkFramebufferStatus(main.gl.FRAMEBUFFER) !== main.gl.FRAMEBUFFER_COMPLETE) floatFb = null;
  main.gl.bindFramebuffer(main.gl.FRAMEBUFFER, null);
}
state.floatFb = floatFb;
return { status: out.status, renderer: out.renderer };
},

frames() {
const main = state.main, floatFb = state.floatFb;
const pixelFrames = [];
const drawTimes = [];
for (const t of CFG.times) {
  setTime(main, t);
  const started = performance.now();
  main.gl.drawArrays(main.gl.TRIANGLES, 0, 3);
  const drawMs = Math.round((performance.now() - started) * 10) / 10;
  const px = new Uint8Array(main.w * main.h * 4);
  main.gl.readPixels(0, 0, main.w, main.h, main.gl.RGBA, main.gl.UNSIGNED_BYTE, px);
  const [mean, std] = stats(px);
  let nanFraction = null;
  if (floatFb) {
    main.gl.bindFramebuffer(main.gl.FRAMEBUFFER, floatFb);
    main.gl.drawArrays(main.gl.TRIANGLES, 0, 3);
    const fp = new Float32Array(main.w * main.h * 4);
    main.gl.readPixels(0, 0, main.w, main.h, main.gl.RGBA, main.gl.FLOAT, fp);
    let bad = 0;
    for (let i = 0; i < fp.length; i++) if (!isFinite(fp[i])) bad++;
    nanFraction = Math.round((bad / fp.length) * 10000) / 10000;
    main.gl.bindFramebuffer(main.gl.FRAMEBUFFER, null);
  }
  out.frames.push({ t, luma_mean: mean, luma_std: std, nan_fraction: nanFraction,
                    moved: pixelFrames.length ? movedShare(pixelFrames[pixelFrames.length - 1], px) : null,
                    png: toPng(px, main.w, main.h), draw_ms: drawMs });
  pixelFrames.push(px);
  drawTimes.push(drawMs);
}

// Late frames for the motion check only, each compared with the first frame: a shader that drifts
// a few pixels a minute looks the same at 0, 1 and 2.5 s.
out.late = [];
for (const t of CFG.late_times) {
  setTime(main, t);
  main.gl.drawArrays(main.gl.TRIANGLES, 0, 3);
  const px = new Uint8Array(main.w * main.h * 4);
  main.gl.readPixels(0, 0, main.w, main.h, main.gl.RGBA, main.gl.UNSIGNED_BYTE, px);
  out.late.push({ t, moved: movedShare(pixelFrames[0], px) });
}

out.contact_sheet = sheet(pixelFrames, CFG.width, CFG.height, 3, CFG.sheet_cell || 384);
out.draw_ms = Math.round((drawTimes.reduce((a, b) => a + b, 0) / drawTimes.length) * 10) / 10;
return { status: out.status };
},

// One second of small frames: luminance and saturated-red share per frame, numbers only.
flash() {
const flash = build(CFG.flash_width, CFG.flash_height);
if (flash === null || flash.error) {
  out.status = 'flash_error';
  out.detail = flash ? flash.error : 'no WebGL2 for the flash pass';
  return out;
}
const luma = [], red = [];
for (let i = 0; i < CFG.flash_frames; i++) {
  setTime(flash, i / CFG.flash_fps);
  flash.gl.drawArrays(flash.gl.TRIANGLES, 0, 3);
  const px = new Uint8Array(flash.w * flash.h * 4);
  flash.gl.readPixels(0, 0, flash.w, flash.h, flash.gl.RGBA, flash.gl.UNSIGNED_BYTE, px);
  luma.push(Math.round(relativeLuminance(px) * 10000) / 10000);
  red.push(redShare(px));
}
out.flash = { lumas: luma, reds: red, fps: CFG.flash_fps };
return out;
},

// The library thumbnail: the t = 1 s frame with the given slider values, drawn into a small
// 2D canvas and encoded as WebP in this session, so no PNG round trip is needed.
thumb() {
const main = state.main;
setTime(main, CFG.thumb_time);
main.gl.drawArrays(main.gl.TRIANGLES, 0, 3);
const small = document.createElement('canvas');
small.width = CFG.thumb_width; small.height = CFG.thumb_height;
const ctx = small.getContext('2d');
ctx.imageSmoothingEnabled = true;
ctx.imageSmoothingQuality = 'high';
ctx.drawImage(main.gl.canvas, 0, 0, small.width, small.height);
out.thumb = small.toDataURL('image/webp', CFG.thumb_quality);
return out;
}
};
return true;
})()"""

_DIAG_RE = re.compile(r"^(ERROR|WARNING): (\d+):(\d+): (.*)$")
_WHERE = {"0": "header", "1": "shader", "2": "footer"}
_FOOTER_HINT = "the shader must define `void mainImage(out vec4 fragColor, in vec2 fragCoord)`"


@dataclass
class Diagnostic:
    severity: str
    where: str
    line: int
    message: str


@dataclass
class RenderResult:
    status: str  # ok | compile_error | link_error | timeout | no_webgl2 | no_browser | browser_error
    compile_log: str = ""
    errors: list[Diagnostic] = field(default_factory=list)
    png_path: str | None = None
    draw_ms: float | None = None
    wall_ms: int | None = None
    luma_mean: float | None = None
    luma_std: float | None = None
    renderer: str | None = None
    detail: str | None = None
    phase: str | None = None          # set on a timeout: start | compile | frames | flash | render
    slot_wait_ms: int = 0             # time spent waiting for a free Chromium slot

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BundleResult:
    status: str
    compile_log: str = ""
    errors: list[Diagnostic] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)   # {t, png_path, luma_mean, luma_std, nan_fraction, moved}
    late: list[dict] = field(default_factory=list)     # {t, moved}: motion-only frames against the first
    contact_sheet: str | None = None
    thumb: str | None = None                            # thumb.webp path
    flash: dict = field(default_factory=dict)
    draw_ms: float | None = None
    wall_ms: int | None = None
    renderer: str | None = None
    detail: str | None = None
    phase: str | None = None          # set on a timeout: start | compile | frames | flash
    slot_wait_ms: int = 0             # time spent waiting for a free Chromium slot

    def as_dict(self) -> dict:
        return asdict(self)


class _Deadline(Exception):
    pass


class _Busy(Exception):
    pass


# At most this many Chromium children per profile at once. Tool calls and dashboard routes run in
# different processes, so the limit is a set of lock files, not a counter in memory.
MAX_CHILDREN = 2
SLOT_WAIT_S = 120.0
_SLOT_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar("shaderbruecke_slots",
                                                                       default=None)


@contextlib.contextmanager
def chromium_slots(directory: Path | str):
    """Count Chromium children against the lock files in `directory` for this block.

    Callers pass a folder under the profile's data dir, so the limit is per profile. Without one,
    a per-user folder in the system temp dir is used.
    """
    token = _SLOT_DIR.set(Path(directory))
    try:
        yield
    finally:
        _SLOT_DIR.reset(token)


def _slot_dir() -> Path:
    chosen = _SLOT_DIR.get()
    if chosen is not None:
        return chosen
    return Path(tempfile.gettempdir()) / f"shaderbruecke-chromium-{os.getuid()}"


@contextlib.contextmanager
def _slot():
    """Hold one of `MAX_CHILDREN` lock files; yields the milliseconds spent waiting."""
    directory = _slot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        for index in range(MAX_CHILDREN):
            fd = os.open(directory / f"chromium-{index}.lock",
                         os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield int((time.monotonic() - started) * 1000)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if time.monotonic() - started >= SLOT_WAIT_S:
            raise _Busy(f"{MAX_CHILDREN} renders were already running for this profile at once "
                        f"and none finished within {SLOT_WAIT_S:g} s; try again in a moment")
        time.sleep(0.1)


def free_slots(directory: Path | str) -> int:
    """How many of the `MAX_CHILDREN` Chromium slots in `directory` are free right now (each is
    tried without waiting and released at once). The Lab's save answers `busy` at 0 instead of
    waiting up to `SLOT_WAIT_S` inside a request."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    free = 0
    for index in range(MAX_CHILDREN):
        fd = os.open(folder / f"chromium-{index}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            free += 1
        except BlockingIOError:
            pass
        finally:
            os.close(fd)
    return free


_PHASE_HINTS = {
    "start": "the browser did not start or load its page in time; the host may be busy, try again",
    "compile": ("the shader did not compile in time; very long unrolled loops or deep function "
                "nesting make the driver slow"),
    "frames": ("drawing the frames did not finish; most likely an unbounded or very long loop "
               "(use constant loop bounds) or too much work per pixel"),
    "flash": ("the flash scan (one second of small frames) did not finish; the shader is too slow "
              "per frame"),
    "render": ("the render did not finish; most likely an unbounded or very long loop (use "
               "constant loop bounds)"),
    "proof": "the proof renders did not finish; the export or the source is too slow per frame",
    "thumb": "drawing the thumbnail frame did not finish; the shader is too slow per frame",
}


def timeout_detail(phase: str, timeout_s: float) -> str:
    hint = _PHASE_HINTS.get(phase, f"the {phase} step did not finish")
    return f"timed out after {timeout_s:g} s in the {phase} phase: {hint}"


# The file the Hermes container image writes with its bundled Chromium's path.
HERMES_CHROMIUM_MARKER = Path("/etc/hermes/chromium-executable-path")
# Where a Mac installs Chromium or Chrome; neither puts the binary on PATH.
APP_CHROMIUM_PATHS = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)


def find_chromium() -> str | None:
    """Env override, then the path Hermes itself records, then PATH, then the Mac app folders."""
    explicit = os.environ.get("SHADERBRUECKE_CHROMIUM")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    if HERMES_CHROMIUM_MARKER.is_file():
        candidate = HERMES_CHROMIUM_MARKER.read_text(encoding="utf-8").strip()
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return next((path for path in APP_CHROMIUM_PATHS if os.access(path, os.X_OK)), None)


# The only variables Chromium inherits. The gateway's environment holds every profile's API keys
# and tokens; none of them may reach the browser process. TMPDIR must not be the user-data-dir
# itself: Chromium crashes at start (SIGTRAP) when it is.
_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

# Chromium keeps a Unix socket under TMPDIR and under the profile; a socket path over ~107 bytes
# makes it exit at start. Hermes points TMPDIR into the profile home, which can be long.
MAX_TEMP_BASE = 60


# Chromium starts through a one-line /bin/sh launcher: posix_spawn cannot change directory or limits.
# The child runs inside its throwaway profile (HOME) with core dumps off, so a crash never drops a
# core file into the gateway's working directory. fds 3/4 are already in place when sh starts, and
# `exec` keeps them, so no redirection with fds above 9 is needed.
_LAUNCHER = "/bin/sh"
_LAUNCH_SCRIPT = 'ulimit -c 0 2>/dev/null; cd "$HOME" || exit 1; exec "$0" "$@"'


def short_temp_base() -> str:
    """The host temp dir when its path is short, else the first short writable fallback."""
    candidates = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"]
    for candidate in candidates:
        if candidate and len(candidate) <= MAX_TEMP_BASE and os.path.isdir(candidate) \
                and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def child_env(profile_dir: str) -> dict[str, str]:
    """Environment for the Chromium child: lookup and locale variables, a short temp dir, and the
    throwaway profile as HOME so the browser writes no dotfiles into the real home."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["TMPDIR"] = short_temp_base()
    env["HOME"] = profile_dir
    return env


def parse_log(log: str) -> list[Diagnostic]:
    out = []
    for raw in log.splitlines():
        m = _DIAG_RE.match(raw.strip().rstrip("\x00"))
        if not m:
            continue
        severity, string, line, message = m.groups()
        where = _WHERE.get(string, f"string {string}")
        if where == "footer":
            message = f"{message} ({_FOOTER_HINT})"
        out.append(Diagnostic(severity.lower(), where, int(line), message.strip()))
    return out


class _Pipe:
    """Minimal CDP client over --remote-debugging-pipe."""

    def __init__(self, write_fd: int, read_fd: int, deadline: float):
        self._w, self._r, self._deadline = write_fd, read_fd, deadline
        # Non-blocking, so a browser that stops reading cannot hold a write past the deadline.
        os.set_blocking(self._w, False)
        self._buf = b""
        self._next_id = 0
        self._torn = False
        self.on_event: Callable[[dict], None] | None = None

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        """Write one command without waiting for its reply; returns its id."""
        self._write(self._message(method, params, session), self._deadline)
        return self._next_id

    def close_browser(self, budget_s: float = 0.5) -> bool:
        """Ask the browser to quit, also after the render's deadline has passed (a timed-out
        render still has to tell its browser to go). True when the whole command was written."""
        if self._torn:
            return False                    # a command cut off mid-write would garble this one
        try:
            self._write(self._message("Browser.close", None, None), time.monotonic() + budget_s)
        except (_Deadline, OSError):
            return False
        return True

    def _message(self, method: str, params: dict | None, session: str | None) -> bytes:
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        return json.dumps(msg).encode() + b"\0"

    def _write(self, message: bytes, deadline: float) -> None:
        data = memoryview(message)
        while data:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self._w], [], left)[1]:
                self._torn = self._torn or len(data) < len(message)
                raise _Deadline()
            try:
                data = data[os.write(self._w, data):]
            except BlockingIOError:
                continue

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        call_id = self.send(method, params, session)
        while True:
            reply = self._read_message()
            if reply.get("id") == call_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            # Events arrive on the same pipe. Only `on_event` (the page server) needs any.
            if "method" in reply and self.on_event is not None:
                self.on_event(reply)

    def _read_message(self) -> dict:
        while b"\0" not in self._buf:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise _Deadline()
            ready, _, _ = select.select([self._r], [], [], left)
            if not ready:
                raise _Deadline()
            chunk = os.read(self._r, 1 << 20)
            if not chunk:
                raise RuntimeError("browser closed the DevTools pipe")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)


def _high_fd(fd: int) -> int:
    """Move `fd` to 10 or above (close-on-exec), so the spawn's dup2 onto 3/4 never collides."""
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def _reap(pid: int, grace_s: float) -> int | None:
    """Exit code once `pid` exits within `grace_s`, else None."""
    until = time.monotonic() + grace_s
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return -1
        if done:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= until:
            return None
        time.sleep(0.05)


# Per Chromium binary: does its sandbox start on this host? Learned from the first start in this
# process. A container that refuses the user namespaces the sandbox needs makes Chromium exit at
# once; only then does the child run with `--no-sandbox`.
_SANDBOX_STARTS: dict[str, bool] = {}


def sandbox_state() -> str:
    """`on`, `off` (this host cannot start Chromium's sandbox) or `unknown` (no start yet)."""
    known = _SANDBOX_STARTS.get(find_chromium() or "")
    return "unknown" if known is None else ("on" if known else "off")


class _Child:
    """One short-lived Chromium child: spawn, talk CDP over the pipe, always kill the group."""

    def __init__(self, out_dir: Path, *, timeout_s: float):
        self.chrome = find_chromium()
        self.out_dir = out_dir
        self.timeout_s = timeout_s
        self.profile = ""
        self.pid = 0
        self.cdp: _Pipe | None = None
        self._cmd_w = self._res_r = -1
        self.started = time.monotonic()
        self.phase = "start"
        self.slot_wait_ms = 0
        self.sandbox = True
        self._slot: contextlib.ExitStack | None = None

    def __enter__(self) -> "_Child":
        if not self.chrome:
            raise FileNotFoundError("no Chromium found (set SHADERBRUECKE_CHROMIUM)")
        self._slot = contextlib.ExitStack()
        # Raises _Busy when every slot stays taken; the deadline starts once a slot is ours.
        self.slot_wait_ms = self._slot.enter_context(_slot())
        try:
            # The sandbox stays on unless this host already showed it cannot start one.
            return self._spawn(sandbox=_SANDBOX_STARTS.get(self.chrome, True))
        except BaseException:
            self._stop()
            raise

    def _first_call(self, method: str, params: dict) -> dict:
        """The first DevTools call. When a sandboxed start of unknown outcome dies before it
        answers, start once more without the sandbox, keeping the same deadline. It shows either as
        a closed pipe or, when the first write comes after the child exited, a broken pipe."""
        try:
            result = self.cdp.call(method, params)
        except (RuntimeError, OSError):
            if not self.sandbox or self.chrome in _SANDBOX_STARTS:
                raise
            deadline = self.started + self.timeout_s
            self._stop(keep_slot=True)
            self._spawn(sandbox=False, deadline=deadline)
            result = self.cdp.call(method, params)
        _SANDBOX_STARTS[self.chrome] = self.sandbox
        return result

    def _spawn(self, *, sandbox: bool, deadline: float | None = None) -> "_Child":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="shaderbruecke-chrome-", dir=short_temp_base())
        cmd_r, cmd_w = (_high_fd(fd) for fd in os.pipe())
        res_r, res_w = (_high_fd(fd) for fd in os.pipe())
        log_fd = os.open(self.out_dir / "browser.log",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        argv = [
            self.chrome, "--headless=new", "--remote-debugging-pipe",
            *([] if sandbox else ["--no-sandbox"]),
            "--disable-dev-shm-usage", "--enable-unsafe-swiftshader",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--disable-sync", "--password-store=basic", "--host-resolver-rules=MAP * ~NOTFOUND",
            # The resolver rule stops HTTP; WebRTC would still send STUN over UDP to an IP literal.
            # With no proxy configured, this policy leaves WebRTC no UDP path at all (the
            # `--force-` spelling some guides use is not a switch in this Chromium and did nothing).
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            f"--user-data-dir={self.profile}", "about:blank",
        ]
        self.sandbox = sandbox
        if deadline is None:
            self.started = time.monotonic()
        self.pid = os.posix_spawn(_LAUNCHER, [_LAUNCHER, "-c", _LAUNCH_SCRIPT, *argv],
                                  child_env(self.profile), setpgroup=0,
                                  file_actions=[
                                      (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
                                      (os.POSIX_SPAWN_DUP2, log_fd, 1),
                                      (os.POSIX_SPAWN_DUP2, log_fd, 2),
                                      (os.POSIX_SPAWN_DUP2, cmd_r, 3),
                                      (os.POSIX_SPAWN_DUP2, res_w, 4),
                                  ])
        for fd in (cmd_r, res_w, log_fd):
            os.close(fd)
        self._cmd_w, self._res_r = cmd_w, res_r
        self.cdp = _Pipe(cmd_w, res_r, self.started + self.timeout_s)
        return self

    def open(self, url: str, serve: Callable[[str], tuple[bytes, str] | None] | None = None) -> str:
        """Create a tab, navigate it to `url`, wait for that document to finish loading, return
        the session.

        With `serve` (url -> (body, mime) or None), every request the tab makes is paused and
        answered by it, or failed when it returns None; nothing reaches the network or the disk.

        A target created with a URL still holds the previous (complete) document for a moment, so
        poll for the *expected* URL as well as `readyState === 'complete'`; otherwise the readiness
        check passes on `about:blank` and the caller evaluates into a half-loaded page.
        """
        if self.cdp is None:
            raise RuntimeError("the browser child was not started")
        target = self._first_call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        if serve is not None:
            self.cdp.on_event = _page_server_handler(self.cdp, session, serve)
            self.cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=session)
        self.cdp.call("Page.enable", {}, session=session)
        self.cdp.call("Page.navigate", {"url": url}, session=session)
        probe = ("JSON.stringify({state: document.readyState, href: location.href, "
                 "errors: (window.sbErrors || []).slice(0, 8)})")
        while True:
            raw = self.cdp.call("Runtime.evaluate", {"expression": probe, "returnByValue": True},
                                session=session).get("result", {}).get("value")
            try:
                info = json.loads(raw) if isinstance(raw, str) else {}
            except ValueError:
                info = {}
            if info.get("href") == url and info.get("state") == "complete":
                return session
            if info.get("state") == "complete" and info.get("errors"):
                raise RuntimeError(f"the page failed to load: {info['errors']}")
            if time.monotonic() > self.started + self.timeout_s:
                raise _Deadline()
            time.sleep(0.05)

    def evaluate(self, expression: str, *, session: str, await_promise: bool = False) -> dict:
        if self.cdp is None:
            raise RuntimeError("the browser child was not started")
        params = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        evaluated = self.cdp.call("Runtime.evaluate", params, session=session)
        if "exceptionDetails" in evaluated:
            detail = evaluated["exceptionDetails"]
            desc = detail.get("exception", {}).get("description")
            return {"status": "exception", "error": desc or detail.get("text")}
        return evaluated.get("result", {}).get("value") or {"status": "browser_error"}

    def __exit__(self, *exc_info) -> None:
        self._stop()

    def _stop(self, *, keep_slot: bool = False) -> None:
        """Close the browser, kill its process group, remove its profile; release the slot unless
        a second start follows."""
        closing = self.cdp is not None and self.cdp.close_browser()
        # A browser told to close gets a moment to quit; one that could not be told is killed now.
        exit_code = _reap(self.pid, 2.0 if closing else 0.0) if self.pid else None
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if exit_code is None and self.pid:
            _reap(self.pid, 5.0)
        for fd in (self._cmd_w, self._res_r):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
        self.cdp, self.pid, self.profile = None, 0, ""
        self._cmd_w = self._res_r = -1
        if self._slot is not None and not keep_slot:
            self._slot.close()
            self._slot = None


def render(source: str, out_dir: Path, *, width: int = 512, height: int = 288, t: float = 1.0,
           timeout_s: float = 20.0) -> RenderResult:
    if not find_chromium():
        return RenderResult("no_browser", detail="no Chromium found (set SHADERBRUECKE_CHROMIUM)")
    cfg = {"header": HEADER, "footer": FOOTER, "vertex": VERTEX, "source": source,
           "width": width, "height": height, "time": t}
    expression = _PROBE_JS.replace("__CFG__", json.dumps(cfg))
    child = _Child(out_dir, timeout_s=timeout_s)
    started = time.monotonic()
    try:
        with child:
            started = child.started
            try:
                session = child.open("about:blank")
                child.phase = "render"
                data = child.evaluate(expression, session=session)
            except _Deadline:
                return RenderResult("timeout", wall_ms=int((time.monotonic() - started) * 1000),
                                    phase=child.phase, slot_wait_ms=child.slot_wait_ms,
                                    detail=timeout_detail(child.phase, timeout_s))
            except (RuntimeError, OSError, ValueError) as exc:
                return RenderResult("browser_error", wall_ms=int((time.monotonic() - started) * 1000),
                                    slot_wait_ms=child.slot_wait_ms,
                                    detail=f"{exc} (see {out_dir / 'browser.log'})")
    except _Busy as exc:
        return RenderResult("busy", detail=str(exc), slot_wait_ms=child.slot_wait_ms)
    except FileNotFoundError as exc:
        return RenderResult("no_browser", detail=str(exc))
    except (RuntimeError, OSError, ValueError) as exc:
        return RenderResult("browser_error", wall_ms=0, detail=f"{exc} (see {out_dir / 'browser.log'})")
    log = (data.get("compile_log") or data.get("link_log") or "").replace("\x00", "").strip()
    result = RenderResult(
        data.get("status", "browser_error"), compile_log=log, errors=parse_log(log),
        draw_ms=data.get("draw_ms"), wall_ms=int((time.monotonic() - started) * 1000),
        luma_mean=data.get("luma_mean"), luma_std=data.get("luma_std"),
        renderer=data.get("renderer"), detail=data.get("error"), slot_wait_ms=child.slot_wait_ms)
    png = data.get("png")
    if result.status == "ok" and isinstance(png, str) and png.startswith("data:image/png;base64,"):
        png_path = out_dir / "frame.png"
        png_path.write_bytes(base64.b64decode(png.split(",", 1)[1]))
        result.png_path = str(png_path)
    return result


def _write_data_url(data: str, path: Path) -> str | None:
    if isinstance(data, str) and data.startswith("data:image/png;base64,"):
        path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
        return str(path)
    return None


THUMB_SIZE = (320, 180)
THUMB_QUALITY = 0.8
THUMB_TIME = 1.0
THUMB_NAME = "thumb.webp"


def render_bundle(source: str, out_dir: Path, *, times: tuple[float, ...] = (0.0, 1.0, 2.5),
                  late_times: tuple[float, ...] = (10.0, 30.0),
                  width: int = 512, height: int = 288, flash_frames: int = 61,
                  flash_fps: float = 60.0, sheet_cell: int = 384,
                  sliders: list[dict] | None = None, values: dict[str, float] | None = None,
                  timeout_s: float = 30.0,
                  phases: tuple[str, ...] = ("compile", "frames", "flash", "thumb")) -> BundleResult:
    """Three frames plus a contact sheet, a NaN pass, a one-second flash scan and the library
    thumbnail (`thumb.webp`, 320x180 at t = 1 s), one browser.

    The steps run as separate evaluations (compile, frames, flash, thumb), so a timeout names the
    phase that was running. `values` overrides the sliders' defaults; the thumbnail uses them too.
    """
    if not find_chromium():
        return BundleResult("no_browser", detail="no Chromium found (set SHADERBRUECKE_CHROMIUM)")
    out_dir.mkdir(parents=True, exist_ok=True)
    header = HEADER
    if sliders:
        declarations = "".join(f"uniform float {slider['name']};\n" for slider in sliders)
        header = HEADER.replace("#line 1 1\n", declarations + "#line 1 1\n")
    slider_values = {slider["name"]: slider["default"] for slider in (sliders or [])}
    slider_values.update({k: v for k, v in (values or {}).items() if k in slider_values})
    cfg = {"header": header, "footer": FOOTER, "vertex": VERTEX, "source": source,
           "width": width, "height": height, "times": list(times), "late_times": list(late_times),
           "sheet_cell": sheet_cell,
           "flash_width": 64, "flash_height": 36, "flash_frames": flash_frames, "flash_fps": flash_fps,
           "sliders": slider_values, "thumb_time": THUMB_TIME, "thumb_width": THUMB_SIZE[0],
           "thumb_height": THUMB_SIZE[1], "thumb_quality": THUMB_QUALITY}
    expression = _BUNDLE_JS.replace("__CFG__", json.dumps(cfg))
    child = _Child(out_dir, timeout_s=timeout_s)
    started = time.monotonic()
    data: dict = {}
    try:
        with child:
            started = child.started
            try:
                session = child.open("about:blank")
                installed = child.evaluate(expression, session=session)
                if installed is not True:
                    raise RuntimeError(f"the bundle did not install: {installed}")
                for phase in phases:
                    child.phase = phase
                    step = child.evaluate(f"window.__sbBundle.{phase}()", session=session)
                    if not isinstance(step, dict):
                        raise RuntimeError(f"the {phase} step returned {step!r}")
                    if step.get("status") == "exception":
                        raise RuntimeError(f"the {phase} step threw: {step.get('error')}")
                    data = step
                    if step.get("status") != "ok":
                        break
            except _Deadline:
                return BundleResult("timeout", wall_ms=int((time.monotonic() - started) * 1000),
                                    phase=child.phase, slot_wait_ms=child.slot_wait_ms,
                                    detail=timeout_detail(child.phase, timeout_s))
            except (RuntimeError, OSError, ValueError) as exc:
                return BundleResult("browser_error", wall_ms=int((time.monotonic() - started) * 1000),
                                    phase=child.phase, slot_wait_ms=child.slot_wait_ms,
                                    detail=f"{exc} (see {out_dir / 'browser.log'})")
    except _Busy as exc:
        return BundleResult("busy", detail=str(exc), slot_wait_ms=child.slot_wait_ms)
    except FileNotFoundError as exc:
        return BundleResult("no_browser", detail=str(exc))
    except (RuntimeError, OSError, ValueError) as exc:
        return BundleResult("browser_error", wall_ms=0, detail=f"{exc} (see {out_dir / 'browser.log'})")

    status = data.get("status", "browser_error")
    log = (data.get("compile_log") or "").replace("\x00", "").strip()
    result = BundleResult(status, compile_log=log, errors=parse_log(log), draw_ms=data.get("draw_ms"),
                          wall_ms=int((time.monotonic() - started) * 1000),
                          renderer=data.get("renderer"), detail=data.get("error") or data.get("detail"),
                          slot_wait_ms=child.slot_wait_ms)
    if status != "ok":
        result.flash = data.get("flash") or {}
        return result
    for frame in data.get("frames", []):
        png_path = _write_data_url(frame.pop("png", ""), out_dir / f"frame-t{frame['t']:g}.png")
        frame["png_path"] = png_path
        result.frames.append(frame)
    result.late = data.get("late") or []
    result.contact_sheet = _write_data_url(data.get("contact_sheet", ""), out_dir / "contact-sheet.png")
    result.thumb = write_webp(data.get("thumb", ""), out_dir / THUMB_NAME)
    result.flash = data.get("flash") or {}
    return result


def write_webp(data: str, path: Path) -> str | None:
    """Write a `data:image/webp` URL; None for anything else (a browser without a WebP encoder
    answers `toDataURL('image/webp')` with a PNG)."""
    if isinstance(data, str) and data.startswith("data:image/webp;base64,"):
        path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
        return str(path)
    return None


def render_thumb(source: str, out_dir: Path, *, sliders: list[dict] | None = None,
                 values: dict[str, float] | None = None, timeout_s: float = 30.0) -> BundleResult:
    """Only the thumbnail: compile, draw t = 1 s with `values`, write `<out_dir>/thumb.webp`.
    Used by the backfill and the refresh after slider tuning."""
    return render_bundle(source, out_dir, sliders=sliders, values=values, timeout_s=timeout_s,
                         phases=("compile", "thumb"))


# The origin a local page is served under. `.invalid` never resolves, so a request the page
# server misses fails instead of reaching a host; https keeps the page a secure context.
PAGE_ORIGIN = "https://shaderbruecke.invalid"
_PAGE_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
               ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
               ".png": "image/png"}


def page_server(root: Path):
    """url -> (bytes, mime) for a file under `root` on PAGE_ORIGIN, else None."""
    root = root.resolve()

    def serve(url: str) -> tuple[bytes, str] | None:
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != PAGE_ORIGIN:
            return None
        try:
            path = (root / unquote(parts.path).lstrip("/")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return None
        except (OSError, ValueError):             # a NUL byte, a name too long for the OS
            return None
        return path.read_bytes(), _PAGE_TYPES.get(path.suffix, "application/octet-stream")

    return serve


def _page_server_handler(cdp: _Pipe, session: str, serve: Callable[[str], tuple[bytes, str] | None]):
    def handle(event: dict) -> None:
        if event.get("method") != "Fetch.requestPaused" or event.get("sessionId") != session:
            return
        params = event["params"]
        found = serve(params["request"]["url"])
        if found is None:
            cdp.send("Fetch.failRequest", {"requestId": params["requestId"],
                                           "errorReason": "BlockedByClient"}, session=session)
            return
        body, mime = found
        cdp.send("Fetch.fulfillRequest", {
            "requestId": params["requestId"], "responseCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": mime}],
            "body": base64.b64encode(body).decode("ascii")}, session=session)

    return handle


def run_page(page: Path | str, expression: str, *, timeout_s: float = 30.0,
             out_dir: Path | None = None, await_promise: bool = True, phase: str = "page") -> dict:
    """Open a page from a local folder in the headless child and evaluate `expression` there.

    `expression` may evaluate to a Promise (await_promise=True). The value must be JSON
    serialisable. Returns the value, or ``{"status": "timeout"|"busy"|"no_browser"|
    "browser_error"|"exception", "detail": ...}``; a timeout names its `phase` (`start` while the
    page loads, then `phase`). The page is served under PAGE_ORIGIN from its own folder over the
    DevTools pipe (`page_server`), so it loads its vendored modules and can read no other local
    file; every other request fails. Without `out_dir` the browser log goes to a temp dir that is
    removed afterwards, never beside the page.
    """
    path = Path(page).resolve()
    if not path.is_file():
        return {"status": "browser_error", "detail": f"page not found: {path}"}
    if out_dir is None:
        scratch = tempfile.TemporaryDirectory(prefix="shaderbruecke-run-")
        try:
            return run_page(path, expression, timeout_s=timeout_s, out_dir=Path(scratch.name),
                            await_promise=await_promise, phase=phase)
        finally:
            scratch.cleanup()
    child = _Child(out_dir, timeout_s=timeout_s)
    started = time.monotonic()
    try:
        with child:
            started = child.started
            try:
                session = child.open(f"{PAGE_ORIGIN}/{quote(path.name)}",
                                     serve=page_server(path.parent))
                child.phase = phase
                value = child.evaluate(expression, session=session, await_promise=await_promise)
            except _Deadline:
                return {"status": "timeout", "phase": child.phase,
                        "wall_ms": int((time.monotonic() - started) * 1000),
                        "slot_wait_ms": child.slot_wait_ms,
                        "detail": timeout_detail(child.phase, timeout_s)}
            except (RuntimeError, OSError, ValueError) as exc:
                return {"status": "browser_error", "phase": child.phase,
                        "wall_ms": int((time.monotonic() - started) * 1000),
                        "detail": f"{exc} (see {out_dir / 'browser.log'})"}
    except _Busy as exc:
        return {"status": "busy", "detail": str(exc), "slot_wait_ms": child.slot_wait_ms}
    except FileNotFoundError as exc:
        return {"status": "no_browser", "detail": str(exc)}
    except (RuntimeError, OSError, ValueError) as exc:
        return {"status": "browser_error", "detail": f"{exc} (see {out_dir / 'browser.log'})"}
    if isinstance(value, dict):
        value.setdefault("wall_ms", int((time.monotonic() - started) * 1000))
        value.setdefault("slot_wait_ms", child.slot_wait_ms)
    return value
