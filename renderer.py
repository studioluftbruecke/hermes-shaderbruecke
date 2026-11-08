"""Compile and snapshot a Shadertoy-style GLSL shader in a short-lived headless Chromium.

The browser is Hermes' own packaged Chromium, spawned as a child process with a throwaway
profile and no network (every host resolves to NOTFOUND). It is driven over the DevTools
pipe (`--remote-debugging-pipe`: JSON messages on fds 3/4, NUL-terminated). One hard
wall-clock deadline covers launch, compile, draw and readback; when it passes, the whole
process group is killed. Shader code never leaves this machine.

`render()` compiles a shader on `about:blank` with the header/footer below and returns one
frame. `render_bundle()` renders the three contact-sheet frames, a NaN pass and a one-second
flash scan in the same browser session. `run_page()` opens a local `file://` page and evaluates
an expression there, which is how the offline three.js proof harness (`headless/harness.html`)
runs: the network stays blocked, the page loads its vendored modules from disk.
"""

from __future__ import annotations

import base64
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

# The M2 bundle: three frames for the contact sheet, a float pass for a NaN verdict, and a
# one-second scan that only returns numbers (the flash verdict is computed in Python).
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
const main = build(CFG.width, CFG.height);
if (main === null) { out.status = 'no_webgl2'; return out; }
if (main.error) { out.status = 'compile_error'; out.compile_log = main.error; return out; }
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
                    png: toPng(px, main.w, main.h), draw_ms: drawMs });
  pixelFrames.push(px);
  drawTimes.push(drawMs);
}

out.contact_sheet = sheet(pixelFrames, CFG.width, CFG.height, 3, CFG.sheet_cell || 384);
out.draw_ms = Math.round((drawTimes.reduce((a, b) => a + b, 0) / drawTimes.length) * 10) / 10;

// One second of small frames: luminance and saturated-red share per frame, numbers only.
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

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BundleResult:
    status: str
    compile_log: str = ""
    errors: list[Diagnostic] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)   # {t, png_path, luma_mean, luma_std, nan_fraction}
    contact_sheet: str | None = None
    flash: dict = field(default_factory=dict)
    draw_ms: float | None = None
    wall_ms: int | None = None
    renderer: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class _Deadline(Exception):
    pass


def find_chromium() -> str | None:
    """Hermes' own Chromium: env override, then the path Hermes itself records, then PATH."""
    explicit = os.environ.get("SHADERBRUECKE_CHROMIUM")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    marker = Path("/etc/hermes/chromium-executable-path")
    if marker.is_file():
        candidate = marker.read_text(encoding="utf-8").strip()
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# The only variables Chromium inherits. The gateway's environment holds every profile's API keys
# and tokens; none of them may reach the browser process. TMPDIR must stay the host's: Chromium
# crashes at start (SIGTRAP) when TMPDIR is its own user-data-dir.
_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")


def child_env(profile_dir: str) -> dict[str, str]:
    """Environment for the Chromium child: lookup, locale and temp-dir variables only, with the
    throwaway profile as HOME so the browser writes no dotfiles into the real home."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
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
        self._buf = b""
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        while True:
            reply = self._read_message()
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            # Events (Page.loadEventFired and friends) arrive on the same pipe; skip them.

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


class _Child:
    """One short-lived Chromium child: spawn, talk CDP over the pipe, always kill the group."""

    def __init__(self, out_dir: Path, *, timeout_s: float, extra_args: tuple[str, ...] = ()):
        self.chrome = find_chromium()
        self.out_dir = out_dir
        self.timeout_s = timeout_s
        self.extra_args = tuple(extra_args)
        self.profile = ""
        self.pid = 0
        self.cdp: _Pipe | None = None
        self._cmd_w = self._res_r = -1
        self.started = time.monotonic()

    def __enter__(self) -> "_Child":
        if not self.chrome:
            raise FileNotFoundError("no Chromium found (set SHADERBRUECKE_CHROMIUM)")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="shaderbruecke-chrome-")
        cmd_r, cmd_w = (_high_fd(fd) for fd in os.pipe())
        res_r, res_w = (_high_fd(fd) for fd in os.pipe())
        log_fd = os.open(self.out_dir / "browser.log",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        argv = [
            self.chrome, "--headless=new", "--remote-debugging-pipe",
            "--no-sandbox", "--disable-dev-shm-usage", "--enable-unsafe-swiftshader",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--disable-sync", "--password-store=basic", "--host-resolver-rules=MAP * ~NOTFOUND",
            *self.extra_args,
            f"--user-data-dir={self.profile}", "about:blank",
        ]
        self.started = time.monotonic()
        self.pid = os.posix_spawn(self.chrome, argv, child_env(self.profile), setpgroup=0,
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

    def open(self, url: str) -> str:
        """Create a tab, navigate it to `url`, wait for that document to finish loading, return
        the session.

        A target created with a URL still holds the previous (complete) document for a moment, so
        poll for the *expected* URL as well as `readyState === 'complete'`; otherwise the readiness
        check passes on `about:blank` and the caller evaluates into a half-loaded page.
        """
        if self.cdp is None:
            raise RuntimeError("the browser child was not started")
        target = self.cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
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
        try:
            if self.cdp is not None:
                self.cdp.call("Browser.close")
        except Exception:
            pass
        exit_code = _reap(self.pid, 2.0) if self.pid else None
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


def render(source: str, out_dir: Path, *, width: int = 512, height: int = 288, t: float = 1.0,
           timeout_s: float = 20.0) -> RenderResult:
    if not find_chromium():
        return RenderResult("no_browser", detail="no Chromium found (set SHADERBRUECKE_CHROMIUM)")
    cfg = {"header": HEADER, "footer": FOOTER, "vertex": VERTEX, "source": source,
           "width": width, "height": height, "time": t}
    expression = _PROBE_JS.replace("__CFG__", json.dumps(cfg))
    try:
        with _Child(out_dir, timeout_s=timeout_s) as child:
            started = child.started
            try:
                session = child.open("about:blank")
                data = child.evaluate(expression, session=session)
            except _Deadline:
                return RenderResult(
                    "timeout", wall_ms=int((time.monotonic() - started) * 1000),
                    detail=(f"the headless render did not finish in {timeout_s:g} s and was killed; "
                            "most likely an unbounded or very long loop (use constant loop bounds)"))
            except (RuntimeError, OSError, ValueError) as exc:
                return RenderResult("browser_error", wall_ms=int((time.monotonic() - started) * 1000),
                                    detail=f"{exc} (see {out_dir / 'browser.log'})")
    except FileNotFoundError as exc:
        return RenderResult("no_browser", detail=str(exc))
    except (RuntimeError, OSError, ValueError) as exc:
        return RenderResult("browser_error", wall_ms=0, detail=f"{exc} (see {out_dir / 'browser.log'})")
    log = (data.get("compile_log") or data.get("link_log") or "").replace("\x00", "").strip()
    result = RenderResult(
        data.get("status", "browser_error"), compile_log=log, errors=parse_log(log),
        draw_ms=data.get("draw_ms"), wall_ms=int((time.monotonic() - started) * 1000),
        luma_mean=data.get("luma_mean"), luma_std=data.get("luma_std"),
        renderer=data.get("renderer"), detail=data.get("error"))
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


def render_bundle(source: str, out_dir: Path, *, times: tuple[float, ...] = (0.0, 1.0, 2.5),
                  width: int = 512, height: int = 288, flash_frames: int = 61,
                  flash_fps: float = 60.0, sheet_cell: int = 384,
                  sliders: list[dict] | None = None,
                  timeout_s: float = 30.0) -> BundleResult:
    """Three frames plus a contact sheet, a NaN pass and a one-second flash scan, one browser."""
    if not find_chromium():
        return BundleResult("no_browser", detail="no Chromium found (set SHADERBRUECKE_CHROMIUM)")
    out_dir.mkdir(parents=True, exist_ok=True)
    header = HEADER
    if sliders:
        declarations = "".join(f"uniform float {slider['name']};\n" for slider in sliders)
        header = HEADER.replace("#line 1 1\n", declarations + "#line 1 1\n")
    cfg = {"header": header, "footer": FOOTER, "vertex": VERTEX, "source": source,
           "width": width, "height": height, "times": list(times), "sheet_cell": sheet_cell,
           "flash_width": 64, "flash_height": 36, "flash_frames": flash_frames, "flash_fps": flash_fps,
           "sliders": {slider["name"]: slider["default"] for slider in (sliders or [])}}
    expression = _BUNDLE_JS.replace("__CFG__", json.dumps(cfg))
    try:
        with _Child(out_dir, timeout_s=timeout_s) as child:
            started = child.started
            try:
                session = child.open("about:blank")
                data = child.evaluate(expression, session=session)
            except _Deadline:
                return BundleResult(
                    "timeout", wall_ms=int((time.monotonic() - started) * 1000),
                    detail=(f"the headless render did not finish in {timeout_s:g} s and was killed; "
                            "most likely an unbounded or very long loop (use constant loop bounds)"))
            except (RuntimeError, OSError, ValueError) as exc:
                return BundleResult("browser_error", wall_ms=int((time.monotonic() - started) * 1000),
                                    detail=f"{exc} (see {out_dir / 'browser.log'})")
    except FileNotFoundError as exc:
        return BundleResult("no_browser", detail=str(exc))
    except (RuntimeError, OSError, ValueError) as exc:
        return BundleResult("browser_error", wall_ms=0, detail=f"{exc} (see {out_dir / 'browser.log'})")

    status = data.get("status", "browser_error")
    log = (data.get("compile_log") or "").replace("\x00", "").strip()
    result = BundleResult(status, compile_log=log, errors=parse_log(log), draw_ms=data.get("draw_ms"),
                          wall_ms=int((time.monotonic() - started) * 1000),
                          renderer=data.get("renderer"), detail=data.get("error") or data.get("detail"))
    if status != "ok":
        result.flash = data.get("flash") or {}
        return result
    for frame in data.get("frames", []):
        png_path = _write_data_url(frame.pop("png", ""), out_dir / f"frame-t{frame['t']:g}.png")
        frame["png_path"] = png_path
        result.frames.append(frame)
    result.contact_sheet = _write_data_url(data.get("contact_sheet", ""), out_dir / "contact-sheet.png")
    result.flash = data.get("flash") or {}
    return result


def run_page(page: Path | str, expression: str, *, timeout_s: float = 30.0,
             out_dir: Path | None = None, await_promise: bool = True) -> dict:
    """Open a local `file://` page in the headless child and evaluate `expression` there.

    `expression` may evaluate to a Promise (await_promise=True). The value must be JSON
    serialisable. Returns the value, or ``{"status": "timeout"|"no_browser"|"browser_error"|
    "exception", "detail": ...}``. The page gets `--allow-file-access-from-files` so its own
    vendored modules load; the network stays blocked.
    """
    path = Path(page).resolve()
    if not path.is_file():
        return {"status": "browser_error", "detail": f"page not found: {path}"}
    out_dir = out_dir or path.parent / ".run"
    try:
        with _Child(out_dir, timeout_s=timeout_s,
                    extra_args=("--allow-file-access-from-files",)) as child:
            started = child.started
            try:
                session = child.open(path.as_uri())
                value = child.evaluate(expression, session=session, await_promise=await_promise)
            except _Deadline:
                return {"status": "timeout", "wall_ms": int((time.monotonic() - started) * 1000),
                        "detail": f"the page did not answer within {timeout_s:g} s and was killed"}
            except (RuntimeError, OSError, ValueError) as exc:
                return {"status": "browser_error",
                        "wall_ms": int((time.monotonic() - started) * 1000),
                        "detail": f"{exc} (see {out_dir / 'browser.log'})"}
    except FileNotFoundError as exc:
        return {"status": "no_browser", "detail": str(exc)}
    except (RuntimeError, OSError, ValueError) as exc:
        return {"status": "browser_error", "detail": f"{exc} (see {out_dir / 'browser.log'})"}
    if isinstance(value, dict):
        value.setdefault("wall_ms", int((time.monotonic() - started) * 1000))
    return value
