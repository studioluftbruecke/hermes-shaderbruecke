/**
 * shaderbruecke desktop half: draws the `.frag` files the `shader_render` tool produced as a
 * live WebGL2 card inside the chat transcript (TOOL_MEDIA_RENDERER_AREA): a header with
 * play/pause and time, the canvas, the shader's sliders, the proof per export target, and
 * copy/export/send actions that go through the plugin API (`/api/plugins/shaderbruecke/...`).
 *
 * The shader already compiled in the headless preflight on the Hermes host, so only checked code
 * reaches this GPU, and a local lint runs before every compile. The header and footer below must
 * match HEADER/FOOTER in renderer.py. Styles are one scoped sheet (`.sb-*` classes on Desktop's
 * `--ui-*` variables), added in register() and removed on dispose, like Desktop's own radio plugin.
 */
import * as sdk from '@hermes/plugin-sdk'
import { memo, useCallback, useEffect, useId, useMemo, useReducer, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'shaderbruecke'

const HEADER = `#version 300 es
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
`

const FOOTER = `
#line 1 2
void main() {
  vec4 color = vec4(0.0, 0.0, 0.0, 1.0);
  mainImage(color, gl_FragCoord.xy);
  sb_FragColor = color;
}
`

const VERTEX = `#version 300 es
in vec2 p;
void main() { gl_Position = vec4(p, 0.0, 1.0); }
`

const SLOW_FRAME_MS = 250
const SLOW_FRAMES_TO_PAUSE = 4
const MAX_PIXEL_RATIO = 1.5

// Mirrors record.py: `// @slider <name> <min>..<max> = <default> [step <s>] ["Human label"]`.
// tests/test_slider_header.py holds both implementations to the same samples.
const SLIDER_RE = /^\s*\/\/\s*@slider\s+([A-Za-z_]\w*)\s+(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)(?:\s+step\s+(-?\d+(?:\.\d+)?))?(?:\s+"([^"]*)")?\s*$/
const SLIDER_RESERVED = new Set(('iTime iResolution iMouse iFrame iDate iTimeDelta fragColor fragCoord main mainImage sb_FragColor ' +
  'void bool int uint float double vec2 vec3 vec4 ivec2 ivec3 ivec4 bvec2 bvec3 bvec4 mat2 mat3 mat4 ' +
  'sampler2D samplerCube const uniform in out inout attribute varying precision highp mediump lowp ' +
  'return if else for while do break continue discard struct switch case default true false ' +
  'texture textureLod texture2D texelFetch smoothstep mix clamp step length distance normalize dot ' +
  'cross sin cos tan asin acos atan pow exp log exp2 log2 sqrt inversesqrt abs sign floor ceil fract ' +
  'mod min max round trunc fwidth dFdx dFdy').split(' '))
const MAX_SLIDERS = 16

// The same table as lint_rules.json; tests/test_lint_rules_in_desktop.py fails when they differ.
const LINT_RULES = [
  { id: 'while-true', kind: 'regex', pattern: '\\bwhile\\s*\\(\\s*(?:true|1)\\s*\\)', severity: 'error',
    message: '`while (true)` cannot be bounded by the compiler and hangs the GPU',
    hint: 'use `for (int i = 0; i < CONSTANT; i++)` with a constant bound instead' },
  { id: 'for-bound', kind: 'for_bound', severity: 'error',
    message: 'this `for` loop has no constant upper bound',
    hint: 'use a constant upper bound, e.g. `for (int i = 0; i < 32; i++)` or a `const int STEPS = 32;`' },
  { id: 'float-counter', kind: 'regex', pattern: '\\bfor\\s*\\(\\s*float\\b', severity: 'warn',
    message: 'a float loop counter accumulates rounding error and can miss its exit',
    hint: 'count with an `int` and divide inside the body' },
  { id: 'webgl1', kind: 'regex', pattern: '\\b(?:texture2D|gl_FragColor|gl_FragData|varying|attribute)\\b', severity: 'error',
    message: 'this is a WebGL1 GLSL ES 1.00 name; the shader is compiled as GLSL ES 3.00',
    hint: '`texture2D` -> `texture`, `gl_FragColor` -> the `fragColor` argument, `varying` -> `in`/`out`, `attribute` -> `in`' },
  { id: 'redeclare', kind: 'regex', pattern: '#version|\\bprecision[ \\t]+(?:lowp|mediump|highp)|\\buniform[ \\t]+\\w+[ \\t]+(?:iTime|iResolution|iMouse|iFrame|iDate|iTimeDelta)\\b', severity: 'error',
    message: '`#version`, `precision` and the i* uniforms are injected by the renderer; declaring them again fails to compile',
    hint: 'delete the declaration and use the uniform as it is' },
  { id: 'own-main', kind: 'regex', pattern: '\\bvoid[ \\t]+main[ \\t]*\\(', severity: 'error',
    message: 'the renderer supplies `main()`; a second one is a redefinition',
    hint: 'write `void mainImage(out vec4 fragColor, in vec2 fragCoord)` only' },
  { id: 'slider-redeclare', kind: 'slider_decl', scope: 'global', severity: 'error',
    message: '`{name}` has a `@slider` line, which already declares `uniform float {name}`; declaring it again fails to compile',
    hint: 'delete this declaration; the slider supplies `{name}`' },
  { id: 'slider-shadow', kind: 'slider_decl', scope: 'local', severity: 'warn',
    message: 'this local `{name}` hides the slider of the same name, so moving the slider changes nothing here',
    hint: 'rename the local variable, or read the slider directly' }
]

// Lint rules whose only failure mode is a hung GPU; any other error is one the compiler rejects.
const STALL_RULES = new Set(['while-true', 'for-bound'])
const SLIDER_NAME_RE = /^[ \t]*\/\/[ \t]*@slider[ \t]+([A-Za-z_]\w*)/gm
const DECL_TYPE = '(?:float|int|uint|bool|[biu]?vec[234]|mat[234](?:x[234])?)'

/** [name, index, scope] for each declaration of a slider name. Mirrors checks._slider_declarations. */
function sliderDeclarations(text, names) {
  const found = []
  const count = (s, c) => s.split(c).length - 1

  for (const name of names) {
    // Slider names are GLSL identifiers, so they need no escaping inside the pattern.
    const pattern = new RegExp(`(?:\\b${DECL_TYPE}[ \\t]+|#[ \\t]*define[ \\t]+)${name}\\b`, 'g')
    let match = pattern.exec(text)

    while (match) {
      const before = text.slice(0, match.index)

      if (count(before, '(') <= count(before, ')')) {
        const nested = count(before, '{') > count(before, '}')
        const global = match[0].trimStart().startsWith('#') || !nested
        found.push([name, match.index, global ? 'global' : 'local'])
      }

      match = pattern.exec(text)
    }
  }

  return found
}

/** Blank out comments, keeping every newline so line numbers stay the agent's. Mirrors checks.py. */
function stripComments(source) {
  const text = String(source || '')
  let out = ''
  let i = 0

  while (i < text.length) {
    const char = text[i]
    const next = text[i + 1]

    if (char === '/' && next === '/') {
      while (i < text.length && text[i] !== '\n') {
        out += ' '
        i += 1
      }
    } else if (char === '/' && next === '*') {
      out += '  '
      i += 2

      while (i < text.length && !(text[i] === '*' && text[i + 1] === '/')) {
        out += text[i] === '\n' ? '\n' : ' '
        i += 1
      }

      if (i < text.length) {
        out += '  '
        i += 2
      }
    } else {
      out += char
      i += 1
    }
  }

  return out
}

function lineAt(text, index) {
  return text.slice(0, index).split('\n').length
}

/** Every `for` header as [index, clauses]. Mirrors checks.py `_for_clauses`. */
function forClauses(text) {
  const found = []
  const pattern = /\bfor\s*\(/g
  let match = pattern.exec(text)

  while (match) {
    const start = text.indexOf('(', match.index)
    let depth = 0
    let i = start

    while (i < text.length) {
      if (text[i] === '(') {
        depth += 1
      } else if (text[i] === ')') {
        depth -= 1
        if (depth === 0) {
          break
        }
      }
      i += 1
    }

    const body = text.slice(start + 1, i)
    const clauses = []
    let current = ''
    depth = 0

    for (const char of body) {
      if (char === '(') {
        depth += 1
      } else if (char === ')') {
        depth -= 1
      }

      if (char === ';' && depth === 0) {
        clauses.push(current)
        current = ''
      } else {
        current += char
      }
    }

    clauses.push(current)
    found.push([match.index, clauses])
    match = pattern.exec(text)
  }

  return found
}

const CONST_RE = /\bconst\s+\w+\s+([A-Za-z_]\w*)\s*=/g
const BOUND_RE = /^\s*[A-Za-z_]\w*\s*(?:<=|>=|<|>)\s*([0-9]+(?:\.[0-9]*)?|[A-Za-z_]\w*)\s*$/

/** The findings for a source: same rules, same order as checks.lint. `sliderNames` are the sliders
 * the card declares as uniforms; by default the names on the source's `@slider` lines. */
function lintSource(source, sliderNames) {
  const stripped = stripComments(source)
  const names = sliderNames || [...new Set(Array.from(String(source || '').matchAll(SLIDER_NAME_RE), m => m[1]))]
  const constants = new Set()
  let constant = CONST_RE.exec(stripped)

  while (constant) {
    constants.add(constant[1])
    constant = CONST_RE.exec(stripped)
  }

  const findings = []

  for (const rule of LINT_RULES) {
    if (rule.kind === 'regex') {
      const pattern = new RegExp(rule.pattern, 'g')
      let hit = pattern.exec(stripped)

      while (hit) {
        findings.push({ rule: rule.id, line: lineAt(stripped, hit.index), severity: rule.severity,
          message: rule.message, hint: rule.hint })

        if (hit.index === pattern.lastIndex) {
          pattern.lastIndex += 1
        }

        hit = pattern.exec(stripped)
      }
    } else if (rule.kind === 'for_bound') {
      for (const [index, clauses] of forClauses(stripped)) {
        if (clauses.length < 2) {
          continue
        }

        const match = BOUND_RE.exec(clauses[1])
        const bound = match ? match[1] : null
        const bounded = Boolean(match) && (/^[0-9]/.test(bound) || constants.has(bound))

        if (!bounded) {
          findings.push({ rule: rule.id, line: lineAt(stripped, index), severity: rule.severity,
            message: rule.message, hint: rule.hint })
        }
      }
    } else if (rule.kind === 'slider_decl') {
      for (const [name, index, scope] of sliderDeclarations(stripped, names)) {
        if (scope === (rule.scope || 'global')) {
          findings.push({ rule: rule.id, line: lineAt(stripped, index), severity: rule.severity,
            message: rule.message.replaceAll('{name}', name), hint: rule.hint.replaceAll('{name}', name) })
        }
      }
    }
  }

  return findings.sort((a, b) => (a.line - b.line) || (a.rule < b.rule ? -1 : 1))
}

function decodeDataUrl(url) {
  const comma = url.indexOf(',')

  if (!url.startsWith('data:') || comma < 0) {
    return null
  }

  const meta = url.slice(5, comma)
  const body = url.slice(comma + 1)

  if (!meta.endsWith(';base64')) {
    return decodeURIComponent(body)
  }

  const bytes = Uint8Array.from(atob(body), c => c.charCodeAt(0))

  return new TextDecoder('utf-8').decode(bytes)
}

function parseResult(result) {
  if (result && typeof result === 'object') {
    return result
  }

  if (typeof result === 'string') {
    try {
      return JSON.parse(result)
    } catch {
      return {}
    }
  }

  return {}
}

/** shader_library actions whose `MEDIA:` line the card draws. A `get`, `list`, `share` or `fork`
 *  result keeps Desktop's plain file: none of them names a `.frag` to play. */
const CARD_LIBRARY_ACTIONS = new Set(['play', 'import'])

/** Whether the producing call draws the card: every shader_render, and shader_library's `play`
 *  and `import` (the action read the way the handler reads it). */
function drawsCard(toolName, args) {
  if (toolName !== 'shader_library') {
    return true
  }

  return CARD_LIBRARY_ACTIONS.has(args && typeof args.action === 'string' ? args.action.toLowerCase() : '')
}

/**
 * What the card needs from the call that produced its `.frag`: id, version, title, sliders and
 * the tuned values to start from. A render keeps its own arguments (the source it rendered, the
 * name it was given). A library `play` or `import` reads everything from the result, because an
 * import's arguments hold the pasted original, not the converted source the frag holds.
 */
function cardMeta(toolName, args, result) {
  const meta = parseResult(result)
  const own = toolName === 'shader_render' && args ? args : {}
  const argsSource = typeof own.source === 'string' ? own.source : ''
  const argsName = typeof own.name === 'string' ? own.name : ''

  return {
    id: typeof meta.id === 'string' ? meta.id : '',
    version: meta.version || null,
    name: argsName || (typeof meta.name === 'string' ? meta.name : ''),
    sliders: Array.isArray(meta.sliders) && meta.sliders.length ? meta.sliders : parseSliders(argsSource),
    tunedValues: meta.tuned_values || meta.slider_values || null,
    argsSource
  }
}

/** Parse `@slider` annotations into {name, min, max, default, step, label}, like record.parse_sliders. */
function parseSliders(source) {
  const sliders = []
  const seen = new Set()

  for (const line of String(source || '').split('\n')) {
    const match = SLIDER_RE.exec(line)

    if (!match) {
      continue
    }

    const name = match[1]
    const min = Number(match[2])
    const max = Number(match[3])
    const value = Number(match[4])

    if (SLIDER_RESERVED.has(name) || seen.has(name) || sliders.length >= MAX_SLIDERS) {
      continue
    }

    if (!(max > min) || value < min || value > max) {
      continue
    }

    sliders.push({ name, min, max, default: value, step: match[5] ? Number(match[5]) : null,
      label: match[6] && match[6].trim() ? match[6].trim() : null })
    seen.add(name)
  }

  return sliders
}

/** The shader header with one `uniform float` per slider, declared before the agent's lines. */
function headerFor(sliders) {
  const declarations = sliders.map(slider => `uniform float ${slider.name};\n`).join('')

  return declarations ? HEADER.replace('#line 1 1\n', declarations + '#line 1 1\n') : HEADER
}

function compile(gl, type, src) {
  const shader = gl.createShader(type)
  gl.shaderSource(shader, src)
  gl.compileShader(shader)

  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    return [shader, '']
  }

  const log = (gl.getShaderInfoLog(shader) || 'compile failed').replace(/\0/g, '').trim()
  gl.deleteShader(shader)

  return [null, log]
}

const IDLE_CONTROL = { play() {}, pause() {}, toggle() {}, stop() {}, setSlider() {}, seek() {}, time: () => 0 }

function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/**
 * ShaderPlayer (P20): the one WebGL2 player behind the chat card, the Shaders page's hover canvas,
 * the detail sheet and the Lab. Framework-free on purpose: React only wraps it.
 *
 * Compile `source` and draw it. The loop runs only while the caller wants motion and the canvas
 * is on screen. Under reduced motion, or with `autoplay: false` (the autoplay rule: an unchecked
 * or flash-flagged shader), it never starts by itself: one still frame at t = 1 s, then `play()`
 * (a click or Space) starts it. `values` are the slider values to start from, so a rebuilt player
 * keeps what the human set.
 */
function createShaderPlayer(canvas, { source, sliders = [], values = null, onState = () => {}, onTime = () => {}, autoplay = true }) {
  const errors = lintSource(source, sliders.map(slider => slider.name)).filter(finding => finding.severity === 'error')

  if (errors.length) {
    onState({ phase: 'lint', findings: errors })

    return IDLE_CONTROL
  }

  const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, premultipliedAlpha: false })

  if (!gl) {
    onState({ phase: 'nowebgl' })

    return IDLE_CONTROL
  }

  // A canvas whose context an earlier player lost hands that dead context back; that is no
  // compile error, and the caller needs a fresh canvas.
  if (gl.isContextLost()) {
    onState({ phase: 'lost' })

    return IDLE_CONTROL
  }

  // Chromium keeps about 16 live contexts and evicts the oldest, so a failed build must give its
  // context back instead of waiting for garbage collection.
  const fail = (state, objects) => {
    for (const object of objects) {
      if (object) {
        object instanceof WebGLProgram ? gl.deleteProgram(object) : gl.deleteShader(object)
      }
    }

    gl.getExtension('WEBGL_lose_context')?.loseContext()
    onState(state)

    return IDLE_CONTROL
  }

  const [vs, vsLog] = compile(gl, gl.VERTEX_SHADER, VERTEX)
  const [fs, fsLog] = compile(gl, gl.FRAGMENT_SHADER, headerFor(sliders || []) + source + FOOTER)

  if (!vs || !fs) {
    return fail({ phase: 'error', log: vsLog || fsLog }, [vs, fs])
  }

  const prog = gl.createProgram()
  gl.attachShader(prog, vs)
  gl.attachShader(prog, fs)
  gl.bindAttribLocation(prog, 0, 'p')
  gl.linkProgram(prog)

  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    return fail({ phase: 'error', log: gl.getProgramInfoLog(prog) || 'link failed' }, [prog, vs, fs])
  }

  gl.useProgram(prog)
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer())
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
  gl.enableVertexAttribArray(0)
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)

  const u = name => gl.getUniformLocation(prog, name)
  const sliderUniforms = {}

  for (const slider of sliders || []) {
    const location = u(slider.name)

    if (location) {
      const value = values && Number.isFinite(values[slider.name]) ? values[slider.name] : slider.default
      gl.uniform1f(location, value)
      sliderUniforms[slider.name] = location
    }
  }

  const loc = {
    res: u('iResolution'),
    time: u('iTime'),
    delta: u('iTimeDelta'),
    frame: u('iFrame'),
    mouse: u('iMouse'),
    date: u('iDate')
  }

  const reduced = prefersReducedMotion() || !autoplay
  const mouse = [0, 0, 0, 0]
  let raf = 0
  let last = 0
  let elapsed = reduced ? 1 : 0
  let slow = 0
  // `running` is the card's wish for motion; `visible` is whether the canvas is on screen.
  let running = !reduced
  let visible = true

  let lost = false
  let stopped = false

  const drawOnce = () => {
    if (lost) {
      return
    }

    const d = new Date()
    gl.viewport(0, 0, canvas.width, canvas.height)
    gl.uniform3f(loc.res, canvas.width, canvas.height, 1)
    gl.uniform1f(loc.time, elapsed)
    gl.uniform1f(loc.delta, 1 / 60)
    gl.uniform1i(loc.frame, Math.round(elapsed * 60))
    gl.uniform4f(loc.mouse, mouse[0], mouse[1], mouse[2], mouse[3])
    gl.uniform4f(loc.date, d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds())
    gl.drawArrays(gl.TRIANGLES, 0, 3)
    onTime(elapsed)
  }

  const onMove = event => {
    const rect = canvas.getBoundingClientRect()
    mouse[0] = (event.clientX - rect.left) * (canvas.width / rect.width)
    mouse[1] = (rect.bottom - event.clientY) * (canvas.height / rect.height)
  }

  canvas.addEventListener('pointermove', onMove)

  // The GPU process or the browser can take the context away (a driver reset, too many
  // contexts); drawing stops and the caller offers a reload on a fresh canvas.
  const onLost = () => {
    if (stopped) {
      return
    }

    lost = true
    running = false
    cancelAnimationFrame(raf)
    raf = 0
    onState({ phase: 'lost' })
  }

  canvas.addEventListener('webglcontextlost', onLost)

  const draw = now => {
    raf = 0

    if (!running || !visible) {
      return
    }

    const dt = last ? (now - last) / 1000 : 1 / 60
    last = now
    elapsed += dt
    slow = dt * 1000 > SLOW_FRAME_MS ? slow + 1 : 0

    if (slow >= SLOW_FRAMES_TO_PAUSE) {
      running = false
      onState({ phase: 'paused', reason: 'slow' })

      return
    }

    drawOnce()
    raf = requestAnimationFrame(draw)
  }

  const schedule = () => {
    if (running && visible && !raf) {
      last = 0
      raf = requestAnimationFrame(draw)
    }
  }

  // Setting the size clears the drawing buffer, so a canvas that is not looping redraws itself.
  const resize = new ResizeObserver(() => {
    const ratio = Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO)
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * ratio))
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * ratio))

    if (!running) {
      drawOnce()
    }
  })

  resize.observe(canvas)

  const seen = new IntersectionObserver(entries => {
    visible = entries.some(entry => entry.isIntersecting)
    schedule()
  })

  seen.observe(canvas)

  drawOnce()
  onState(reduced ? { phase: 'still' } : { phase: 'live' })
  schedule()

  const control = {
    play() {
      if (lost) {
        return
      }

      running = true
      slow = 0
      onState({ phase: 'live' })
      schedule()
    },
    pause() {
      running = false
      cancelAnimationFrame(raf)
      raf = 0
      onState({ phase: 'paused', reason: 'user' })
    },
    toggle() {
      if (running) {
        control.pause()
      } else {
        control.play()
      }
    },
    setSlider(name, value) {
      const location = sliderUniforms[name]

      if (location) {
        gl.uniform1f(location, value)

        if (!running) {
          drawOnce()
        }
      }
    },
    seek(seconds) {
      elapsed = Math.max(0, Number(seconds) || 0)

      if (!running) {
        drawOnce()
      }
    },
    time: () => elapsed,
    stop() {
      stopped = true
      running = false
      cancelAnimationFrame(raf)
      resize.disconnect()
      seen.disconnect()
      canvas.removeEventListener('pointermove', onMove)
      canvas.removeEventListener('webglcontextlost', onLost)
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  }

  return control
}

/** One API handle for the pane; `register(ctx)` sets it. */
let pluginApi = null

function apiCall(path, options) {
  if (!pluginApi || typeof pluginApi.rest !== 'function') {
    return Promise.reject(new Error('the plugin API is not available'))
  }

  return pluginApi.rest(path, options)
}

/** `/shaders/<id>` with the id escaped; ids are folder names and may hold any character. */
function shaderPath(id) {
  return `/shaders/${encodeURIComponent(id)}`
}

/** The slider-values route of one shader; with a version the values land on that version, without
 *  one on the latest (the Shaders page). */
function sliderValuesPath(id, version) {
  return `${shaderPath(id)}/slider_values${version ? `?version=${encodeURIComponent(version)}` : ''}`
}

/** A short stable hash (FNV-1a) of a text, for React keys that must change with the source. */
function textHash(text) {
  let hash = 0x811c9dc5
  const value = String(text || '')

  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }

  return (hash >>> 0).toString(36)
}

const COMMIT_DELAY_MS = 400
const TOAST_MS = 3000
const ACTION_TOAST_MS = 10000

/** The signature a GL program depends on. Equal signatures never rebuild the renderer. */
function sliderSignature(sliders) {
  return sliders.map(s => `${s.name}:${s.min}:${s.max}:${s.default}`).join('|')
}

/** The values a card starts from: the stored tuned values, else the authored defaults. */
function startingValues(sliders, tuned) {
  const values = {}

  for (const slider of sliders) {
    const value = Number((tuned || {})[slider.name])
    values[slider.name] = Number.isFinite(value) && value >= slider.min && value <= slider.max ? value : slider.default
  }

  return values
}

const THUMB_REFRESH_MS = 4000

/**
 * A slider save re-renders the latest thumb in a background job without touching updatedAt, so
 * the library and the shader's detail are asked again now (the answer lists the job) and once
 * more a few seconds later, when the new thumbStamp is usually there.
 */
function refreshAfterTuning(id) {
  const client = sdk.queryClient

  if (!client || typeof client.invalidateQueries !== 'function') {
    return
  }

  const refresh = () => {
    client.invalidateQueries({ queryKey: qkey('library') })
    client.invalidateQueries({ queryKey: qkey('detail', id) })
    client.invalidateQueries({ queryKey: qkey('version', id) })
  }

  refresh()
  window.setTimeout(refresh, THUMB_REFRESH_MS)
}

/**
 * The slider values of one stored shader, shared by the chat card and the detail sheet: a move
 * reaches the player at once and is saved (POST /slider_values) once after COMMIT_DELAY_MS, on
 * blur or on unmount; Reset all DELETEs them. Without an `id` nothing is saved. The card passes
 * the `version` it plays, so its values land there; the sheet passes none (the latest).
 * `notify(kind, error)` words the outcome: 'saved', 'save-failed', 'reset' or 'reset-failed'.
 */
function useSliderCommit({ id, version = null, sliders, initial, controlRef, notify }) {
  const [values, setValues] = useState(() => startingValues(sliders, initial))
  const valuesRef = useRef(values)
  const timer = useRef(0)
  const dirty = useRef(false)
  const touched = useRef(false)
  const latest = useRef({ id, version, notify, sliders })
  latest.current = { id, version, notify, sliders }

  const push = next => {
    for (const [name, value] of Object.entries(next)) {
      controlRef.current && controlRef.current.setSlider(name, value)
    }
  }

  const replace = (next, toPlayer) => {
    valuesRef.current = next
    setValues(next)

    if (toPlayer) {
      push(next)
    }
  }

  const flush = () => {
    window.clearTimeout(timer.current)
    timer.current = 0
    const target = latest.current.id

    if (!dirty.current || !target) {
      return
    }

    dirty.current = false
    apiCall(sliderValuesPath(target, latest.current.version), { method: 'POST', body: { values: valuesRef.current } })
      .then(() => {
        latest.current.notify('saved')
        refreshAfterTuning(target)
      })
      .catch(error => latest.current.notify('save-failed', error))
  }

  const flushRef = useRef(flush)
  flushRef.current = flush
  useEffect(() => () => flushRef.current(), [])

  const set = (name, value, now) => {
    touched.current = true
    replace({ ...valuesRef.current, [name]: value }, false)
    controlRef.current && controlRef.current.setSlider(name, value)
    dirty.current = true
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => flushRef.current(), now ? 0 : COMMIT_DELAY_MS)
  }

  const resetAll = () => {
    window.clearTimeout(timer.current)
    dirty.current = false
    touched.current = true
    replace(startingValues(latest.current.sliders, {}), true)
    const target = latest.current.id

    if (!target) {
      return
    }

    apiCall(sliderValuesPath(target, latest.current.version), { method: 'DELETE' })
      .then(() => {
        latest.current.notify('reset')
        refreshAfterTuning(target)
      })
      .catch(error => latest.current.notify('reset-failed', error))
  }

  return { values, valuesRef, touched, set, resetAll, replace, flush: () => flushRef.current() }
}

/** `sunHeight` reads "Sun height", `cloud_cover` "Cloud cover", `glowRGB` "Glow RGB". */
function humanize(name) {
  const words = String(name)
    .replace(/_+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .trim()
    .split(/\s+/)
    .map(word => (word.length > 1 && word === word.toUpperCase() ? word : word.toLowerCase()))
  const text = words.join(' ')

  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Decimals a slider value shows: the step's own decimals, else from the range. */
function decimalsFor(slider) {
  if (slider.step) {
    const text = String(slider.step)
    const dot = text.indexOf('.')

    return dot < 0 ? 0 : Math.min(4, text.length - dot - 1)
  }

  const range = slider.max - slider.min

  return range >= 100 ? 0 : range >= 10 ? 1 : 2
}

function capitalize(text) {
  const value = String(text || '')

  return value.charAt(0).toUpperCase() + value.slice(1)
}

function formatTime(seconds) {
  const t = Math.max(0, seconds || 0)
  const minutes = Math.floor(t / 60)
  const rest = (t - minutes * 60).toFixed(2).padStart(5, '0')

  return `${minutes}:${rest}`
}

const TARGET_NAMES = {
  three: ['three.js', 'three.js material'],
  r3f: ['R3F', 'React Three Fiber'],
  tsl: ['TSL', 'TSL module'],
  shadertoy: ['Shadertoy', 'Shadertoy tab'],
  td: ['TouchDesigner', 'TouchDesigner GLSL TOP'],
  isf: ['ISF', 'ISF shader'],
  agent: ['Agent', 'Coding agent (three.js)']
}

const AGENT_HINT = 'Paste into Cursor, Claude Code or Codex in your web project.'

function targetName(row, long) {
  const names = TARGET_NAMES[row.id]

  return names ? names[long ? 1 : 0] : row.label || row.id
}

/** The request an "Ask Hermes to fix it" toast action drafts after a failed or unproven export. */
function exportFixRequest(title, id, version, row, problem) {
  return `The ${targetName(row, true)} export of the shader "${title}" (id ${id}${version ? `, v${version}` : ''}) ${problem}. Fix it with shader_export and prove it.`
}

/** A target can be copied when its file exists, or always for the agent handoff (generated). */
function available(row) {
  return row.exported || row.id === 'agent'
}

/** The verdict a chip shows, in words: match, differs, not proven, not exported. */
function verdictOf(row) {
  if (!available(row)) {
    return { word: 'not exported', tone: 'none', icon: 'circle-large-outline' }
  }

  const verdict = row.proof && row.proof.verdict

  if (verdict === 'match') {
    return { word: 'match', tone: 'ok', icon: 'pass' }
  }

  if (verdict === 'differs') {
    return { word: 'differs', tone: 'warn', icon: 'warning' }
  }

  return { word: 'not proven', tone: 'none', icon: 'question' }
}

const CSS = `
.sb-card{margin:.5rem 0;max-width:640px;border:1px solid var(--ui-stroke-tertiary);border-radius:12px;background:var(--ui-bg-elevated);font-size:13px;line-height:1.45;color:var(--ui-text-primary)}
.sb-card *{box-sizing:border-box}
.sb-head{display:flex;align-items:center;gap:8px;padding:8px 12px 8px 8px;min-width:0}
.sb-icon-btn{flex:none;width:28px;height:28px;display:inline-grid;place-items:center;border-radius:8px;border:0;background:transparent;color:var(--ui-text-primary);cursor:pointer;transition:background-color .15s ease-out}
.sb-icon-btn:hover{background:var(--ui-control-hover-background)}
.sb-icon-btn:active{transform:translateY(1px)}
.sb-icon-btn:disabled{color:var(--ui-text-quaternary);cursor:default;background:transparent}
.sb-name{font-weight:600;letter-spacing:-.005em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.sb-ver{flex:none;font:500 11px/1 var(--dt-font-mono);color:var(--ui-text-secondary);padding:3px 5px;border-radius:5px;background:var(--ui-bg-tertiary)}
.sb-state{flex:none;display:inline-flex;align-items:center;gap:6px;margin-left:auto;font-size:12px;color:var(--ui-text-secondary)}
.sb-dot{width:6px;height:6px;border-radius:50%;background:var(--ui-text-quaternary)}
.sb-card[data-shaderbruecke=live] .sb-dot{background:var(--ui-green)}
.sb-card[data-shaderbruecke=lint] .sb-dot,.sb-card[data-shaderbruecke=error] .sb-dot{background:var(--ui-red)}
.sb-time{flex:none;min-width:4.6em;text-align:right;font:12px/1 var(--dt-font-mono);font-variant-numeric:tabular-nums;color:var(--ui-text-secondary)}
.sb-stage{position:relative;margin:0 8px;border-radius:8px;overflow:hidden;background:#0b0b0d}
.sb-canvas{display:block;width:100%;aspect-ratio:16/9;cursor:pointer;outline:none}
.sb-stage:has(.sb-canvas:focus-visible){outline:2px solid var(--ui-accent);outline-offset:2px}
.sb-hint{position:absolute;left:50%;bottom:10px;transform:translate(-50%,4px);padding:4px 10px;border-radius:999px;font-size:11.5px;color:#f4f4f5;background:rgb(12 12 14/.66);pointer-events:none;white-space:nowrap;opacity:0;transition:opacity .18s ease-out,transform .18s ease-out}
.sb-stage:hover .sb-hint,.sb-stage:focus-within .sb-hint{opacity:1;transform:translate(-50%,0)}
.sb-card[data-shaderbruecke=paused] .sb-hint,.sb-card[data-shaderbruecke=still] .sb-hint{bottom:38px}
.sb-scrub{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:8px;padding:14px 10px 6px;background:linear-gradient(to top,rgb(0 0 0/.55),transparent)}
.sb-scrub input{flex:1;margin:0;accent-color:#f4f4f5;cursor:ew-resize}
.sb-scrub span{font:11px/1 var(--dt-font-mono);color:#f4f4f5;font-variant-numeric:tabular-nums}
.sb-problem{margin:0 8px;border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;background:var(--ui-bg-tertiary);color:var(--ui-text-primary)}
.sb-problem h4{margin:0;font-size:13px;font-weight:600;display:flex;gap:6px;align-items:center}
.sb-problem p{margin:0;color:var(--ui-text-secondary);font-size:12.5px;max-width:62ch}
.sb-problem pre{margin:0;max-height:140px;overflow:auto;white-space:pre-wrap;font:11.5px/1.5 var(--dt-font-mono);color:var(--ui-text-secondary)}
.sb-section{padding:10px 12px 2px}
.sb-sec-head{display:flex;justify-content:space-between;align-items:center;min-height:22px;margin-bottom:2px}
.sb-sec-title{font-size:12px;color:var(--ui-text-secondary)}
.sb-link{border:0;background:none;padding:2px 6px;margin-right:-6px;border-radius:6px;color:var(--ui-text-secondary);font:inherit;font-size:12px;cursor:pointer}
.sb-link:hover{color:var(--ui-text-primary);background:var(--ui-control-hover-background)}
.sb-row{display:grid;grid-template-columns:minmax(6rem,9rem) 1fr 3.4rem;align-items:center;gap:12px;min-height:28px}
.sb-label{color:var(--ui-text-secondary);cursor:default;user-select:none;overflow-wrap:break-word;line-height:1.25}
.sb-label[data-tuned=true]{color:var(--ui-text-primary)}
.sb-val{font:12px/1 var(--dt-font-mono);text-align:right;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.sb-range{-webkit-appearance:none;appearance:none;width:100%;height:20px;margin:0;background:transparent;accent-color:var(--ui-accent);cursor:pointer}
.sb-range::-webkit-slider-runnable-track{height:4px;border-radius:4px;background:linear-gradient(to right,var(--ui-accent) var(--fill),var(--ui-stroke-secondary) var(--fill))}
.sb-range::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;margin-top:-5px;border-radius:50%;background:var(--ui-bg-elevated);border:2px solid var(--ui-accent);box-shadow:0 1px 2px rgb(0 0 0/.25);transition:transform .12s ease-out}
.sb-range:active::-webkit-slider-thumb{transform:scale(1.15)}
.sb-range::-moz-range-track{height:4px;border-radius:4px;background:var(--ui-stroke-secondary)}
.sb-range::-moz-range-progress{height:4px;border-radius:4px;background:var(--ui-accent)}
.sb-range::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:var(--ui-bg-elevated);border:2px solid var(--ui-accent)}
.sb-proof{display:flex;flex-wrap:wrap;gap:4px;padding:10px 12px;margin-top:8px;border-top:1px solid var(--ui-stroke-tertiary)}
.sb-chip{display:inline-flex;align-items:center;gap:4px;padding:2px 7px 2px 5px;border-radius:6px;font-size:11.5px;background:var(--ui-bg-tertiary);color:var(--ui-text-primary);white-space:nowrap}
.sb-chip .codicon{font-size:13px}
.sb-ok{color:color-mix(in srgb,var(--ui-green) 72%,var(--ui-base))}
.sb-warn{color:color-mix(in srgb,var(--ui-yellow) 62%,var(--ui-base))}
.sb-none{color:var(--ui-text-secondary)}
.sb-err{color:color-mix(in srgb,var(--ui-red) 80%,var(--ui-base))}
.sb-actions{position:relative;display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:0 12px 12px}
.sb-actions-only{padding-top:12px}
.sb-btn{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:8px;font:inherit;font-size:12.5px;white-space:nowrap;border:1px solid var(--ui-stroke-secondary);background:transparent;color:var(--ui-text-primary);cursor:pointer;transition:background-color .15s ease-out}
.sb-btn:hover{background:var(--ui-control-hover-background)}
.sb-btn:active{transform:translateY(1px)}
.sb-btn:disabled{opacity:.55;cursor:default;transform:none}
.sb-btn .codicon{font-size:14px}
.sb-primary{background:var(--dt-primary-solid,var(--ui-accent));border-color:transparent;color:var(--dt-primary-solid-foreground,#fcfcfc);font-weight:500}
.sb-primary:hover{background:color-mix(in srgb,var(--dt-primary-solid,var(--ui-accent)) 88%,#000)}
.sb-ghost{border-color:transparent;color:var(--ui-text-secondary)}
.sb-ghost:hover{color:var(--ui-text-primary)}
.sb-ghost[aria-expanded=true]{color:var(--ui-text-primary);background:var(--ui-control-hover-background)}
.sb-toast{margin-left:auto;display:inline-flex;align-items:center;gap:6px;min-height:28px;font-size:12px;color:var(--ui-text-secondary);max-width:100%}
.sb-toast .sb-btn{height:24px;padding:0 8px;font-size:12px}
.sb-toast-text{min-width:0}
.sb-menu{padding:4px;border-radius:10px;background:var(--ui-bg-elevated);color:var(--ui-text-primary);font-size:13px;line-height:1.45;box-shadow:0 14px 36px rgb(0 0 0/.3),0 2px 6px rgb(0 0 0/.12)}
.sb-menu *{box-sizing:border-box}
.sb-menu :focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.sb-mstatus{padding:6px 8px;color:var(--ui-text-secondary)}
.sb-mrow{display:flex;flex-wrap:wrap;align-items:center;gap:2px 8px;padding:6px 6px 6px 8px;border-radius:7px}
.sb-mmain{flex:1 1 12rem;min-width:0}
.sb-mrow:hover,.sb-mrow:focus-within{background:var(--ui-control-hover-background)}
.sb-mname{font-weight:500;display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.sb-mname small{font-size:11.5px;font-weight:400}
.sb-mfile{font:11px/1.4 var(--dt-font-mono);color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-mhint{font-size:11.5px;color:var(--ui-text-secondary)}
.sb-mact{display:flex;gap:2px;margin-left:auto}
.sb-mact .sb-btn{height:26px;padding:0 8px;font-size:12px}
.sb-code{margin:0 12px 12px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;max-height:320px;overflow:auto;background:var(--ui-bg-tertiary)}
.sb-code ol{list-style:none;margin:0;padding:6px 0;font:11.5px/1.6 var(--dt-font-mono);counter-reset:sb-line}
.sb-code li{display:grid;grid-template-columns:3.2em 1fr;white-space:pre;color:var(--ui-text-primary)}
.sb-code li>span:first-child{text-align:right;padding-right:12px;color:var(--ui-text-secondary);user-select:none}
.sb-code li[data-mark=error]{background:color-mix(in srgb,var(--ui-red) 14%,transparent)}
.sb-code li[data-mark=warn]{background:color-mix(in srgb,var(--ui-yellow) 14%,transparent)}
.sb-code .sb-finding{grid-column:2;white-space:pre-wrap;font-family:var(--dt-font-sans);font-size:11.5px;padding:2px 0 4px;color:var(--ui-text-secondary)}
.sb-card :focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.sb-range:focus-visible{outline-offset:3px;border-radius:6px}
.sb-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){.sb-card *{transition:none!important}}
`

let styleNode = null

function mountStyles() {
  if (styleNode || typeof document === 'undefined') {
    return
  }

  styleNode = document.createElement('style')
  styleNode.setAttribute('data-plugin', ID)
  styleNode.textContent = CSS + PAGE_CSS + DETAIL_CSS + LAB_CSS
  document.head.append(styleNode)
}

function unmountStyles() {
  styleNode?.remove()
  styleNode = null
}

function icon(name, className) {
  return jsx('i', { 'aria-hidden': 'true', className: `codicon codicon-${name}${className ? ' ' + className : ''}` })
}

function SliderRow({ slider, value, onChange, onBlur, onReset }) {
  const label = slider.label || humanize(slider.name)
  const id = `sb-${slider.name}-${useId()}`
  const fill = ((value - slider.min) / (slider.max - slider.min)) * 100
  const decimals = decimalsFor(slider)
  const shown = Number(value).toFixed(decimals)
  const tuned = Math.abs(value - slider.default) > 1e-9

  return jsxs('div', {
    className: 'sb-row',
    children: [
      jsx('label', {
        htmlFor: id,
        className: 'sb-label',
        'data-tuned': tuned ? 'true' : 'false',
        title: `${label} (${slider.name}). Double-click to reset to ${Number(slider.default).toFixed(decimals)}.`,
        onDoubleClick: onReset,
        children: label
      }),
      jsx('input', {
        id,
        className: 'sb-range',
        type: 'range',
        min: slider.min,
        max: slider.max,
        step: slider.step || 'any',
        value,
        'aria-valuetext': shown,
        style: { '--fill': `${fill}%` },
        onChange: event => onChange(slider.name, Number(event.target.value)),
        onBlur
      }),
      jsx('output', { htmlFor: id, className: 'sb-val', children: shown })
    ]
  })
}

function CodeView({ source, findings }) {
  const marks = new Map()

  for (const finding of findings) {
    marks.set(finding.line, [...(marks.get(finding.line) || []), finding])
  }

  return jsx('div', {
    className: 'sb-code',
    role: 'region',
    'aria-label': 'Shader source',
    tabIndex: 0,
    children: jsx('ol', {
      children: String(source || '').split('\n').map((line, index) => {
        const own = marks.get(index + 1) || []
        const mark = own.some(f => f.severity === 'error') ? 'error' : own.length ? 'warn' : undefined

        return jsxs('li', {
          'data-mark': mark,
          children: [
            jsx('span', { children: index + 1 }),
            jsx('span', { children: line || ' ' }),
            ...own.map((f, i) => jsxs('span', {
              className: 'sb-finding',
              children: [icon(f.severity === 'error' ? 'error' : 'warning', f.severity === 'error' ? 'sb-err' : 'sb-warn'), ` ${capitalize(f.message)}. ${capitalize(f.hint)}.`]
            }, i))
          ]
        }, index)
      })
    })
  })
}

const MENU_STYLE = { width: 'min(440px, calc(100vw - 32px))', maxHeight: 'var(--radix-popover-content-available-height)', overflowY: 'auto' }

/**
 * The Export menu of the chat card and the detail sheet, in Desktop's Popover: Radix places it
 * inside the window and owns Escape, outside clicks and the focus return to the trigger.
 * `refocusRef.current = false` before a close keeps focus where an action put it (the composer).
 */
function ExportPopover({ id, open, onOpenChange, refocusRef, trigger, children }) {
  return jsxs(sdk.Popover, {
    open,
    onOpenChange,
    children: [
      jsx(sdk.PopoverTrigger, { asChild: true, children: trigger }),
      jsx(sdk.PopoverContent, {
        id,
        side: 'bottom',
        align: 'end',
        collisionPadding: 12,
        className: 'sb-menu',
        style: MENU_STYLE,
        onCloseAutoFocus: event => {
          if (!refocusRef.current) {
            event.preventDefault()
          }

          refocusRef.current = true
        },
        children
      })
    ]
  })
}

function ExportMenu({ targets, busy, onCopy, onSend, onExport }) {
  return jsx('div', {
    role: 'group',
    'aria-label': 'Export targets',
    children: targets.map(row => {
      const verdict = verdictOf(row)
      const name = targetName(row, true)
      const ready = available(row)
      const working = busy === row.id

      return jsxs('div', {
        className: 'sb-mrow',
        'data-target': row.id,
        children: [
          jsxs('div', {
            className: 'sb-mmain',
            children: [
              jsxs('div', {
                className: 'sb-mname',
                children: [
                  name,
                  jsx('small', { className: `sb-${verdict.tone}`, children: verdict.word }),
                  row.status === 'experimental' ? jsx('small', { className: 'sb-none', children: 'experimental' }) : null
                ]
              }),
              row.id === 'agent'
                ? jsx('div', { className: 'sb-mhint', children: AGENT_HINT })
                : jsx('div', { className: 'sb-mfile', title: row.proof && row.proof.why ? row.proof.why : undefined, children: row.main_file || row.label })
            ]
          }),
          jsx('div', {
            className: 'sb-mact',
            children: ready
              ? [
                  jsx('button', { key: 'copy', type: 'button', className: 'sb-btn sb-ghost', 'aria-label': `Copy ${name} file`, onClick: () => onCopy(row), children: 'Copy file' }),
                  jsx('button', { key: 'send', type: 'button', className: 'sb-btn sb-ghost', 'aria-label': `Send ${name} to chat`, onClick: () => onSend(row), children: 'Send to chat' })
                ]
              : jsx('button', { type: 'button', className: 'sb-btn sb-ghost', disabled: Boolean(busy), 'aria-label': `Export ${name} now`, onClick: () => onExport(row),
                  children: working ? [icon('loading', 'codicon-modifier-spin'), ' Exporting'] : 'Export now' })
          })
        ]
      })
    })
  })
}

function cardReducer(state, action) {
  return action.phase === state.phase && !action.findings && !action.log && (action.reason || '') === state.reason
    ? state
    : { phase: action.phase, reason: action.reason || '', findings: action.findings || null, log: action.log || '' }
}

const STATE_WORDS = {
  loading: 'Loading',
  live: 'Live',
  still: 'Still frame',
  paused: 'Paused',
  lint: 'Not compiled',
  error: 'Compile error',
  lost: 'Preview lost'
}

function ShaderCanvas({ file, toolName, args, result, fallback, sessionId }) {
  const canvasRef = useRef(null)
  const controlRef = useRef(null)
  const timeRef = useRef(null)
  const refocusRef = useRef(true)
  const [source, setSource] = useState(null)
  const [state, dispatch] = useReducer(cardReducer, { phase: 'loading', reason: '', findings: null, log: '' })
  const [toast, setToast] = useState(null)
  const [info, setInfo] = useState(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [codeOpen, setCodeOpen] = useState(false)
  const [busy, setBusy] = useState('')
  const [scrub, setScrub] = useState(1)
  // A live turn hands `result` over as a JSON string, a reloaded transcript as an object. Parse
  // once per distinct raw value, so a host re-render never produces new arrays.
  // The host may build a new `args` object per render (the deferred-tool bridge), so the memo keys
  // on the two strings a render's card reads from it.
  const argsSourceKey = args && typeof args.source === 'string' ? args.source : ''
  const argsNameKey = args && typeof args.name === 'string' ? args.name : ''
  const card = useMemo(
    () => cardMeta(toolName, { source: argsSourceKey, name: argsNameKey }, result),
    [toolName, argsSourceKey, argsNameKey, result]
  )
  const { argsSource } = card
  const shaderId = card.id
  const version = card.version || (info && info.version) || null
  const versionQuery = card.version ? `?version=${encodeURIComponent(card.version)}` : ''
  const rawTitle = card.name || (info && info.name) || file.name
  const title = rawTitle ? rawTitle.charAt(0).toUpperCase() + rawTitle.slice(1) : 'Shader'
  const parsedSliders = card.sliders
  const signature = sliderSignature(parsedSliders)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const sliders = useMemo(() => parsedSliders, [signature])
  const [reload, setReload] = useState(0)
  const menuId = `sb-export-menu-${useId()}`
  const toastTimer = useRef(0)
  const targets = info && Array.isArray(info.targets) ? info.targets : []

  const say = (text, tone = 'none', actions = null) => {
    window.clearTimeout(toastTimer.current)
    setToast({ text, tone, actions })
    toastTimer.current = window.setTimeout(() => setToast(null), actions ? ACTION_TOAST_MS : TOAST_MS)
  }

  useEffect(() => () => window.clearTimeout(toastTimer.current), [])

  const tuning = useSliderCommit({
    id: shaderId,
    version: card.version,
    sliders,
    initial: card.tunedValues,
    controlRef,
    notify: (kind, error) => {
      if (kind === 'saved' || kind === 'reset') {
        say(kind === 'saved' ? 'Saved, used in exports' : 'Back to the authored defaults, used in exports', 'ok')
      } else {
        const why = restProblem(error).message || 'the plugin API did not answer'
        say(kind === 'save-failed' ? `Not saved: ${why}. Try again in a moment.` : `Reset not saved: ${why}.`, 'err')
      }
    }
  })
  const { values, valuesRef } = tuning

  useEffect(() => {
    let live = true

    file
      .resolveUrl()
      .then(url => (typeof url === 'string' ? decodeDataUrl(url) : null))
      .catch(() => null)
      .then(text => {
        if (!live) {
          return
        }

        const src = text || argsSource

        if (src) {
          setSource(src)
        } else {
          dispatch({ phase: 'unavailable' })
        }
      })

    return () => {
      live = false
    }
  }, [file, argsSource])

  const loadTargets = () => {
    if (!shaderId) {
      return Promise.resolve(null)
    }

    return apiCall(`${shaderPath(shaderId)}/targets${versionQuery}`)
      .then(payload => {
        if (payload && Array.isArray(payload.targets)) {
          setInfo(payload)
        }

        return payload
      })
      .catch(() => null)
  }

  useEffect(() => {
    let live = true

    loadTargets().then(payload => {
      // The stored tuned values win over the ones the tool result carried, unless the human
      // already moved a slider in this card.
      if (live && payload && payload.tuned_values && !tuning.touched.current) {
        tuning.replace(startingValues(sliders, payload.tuned_values), true)
      }
    })

    return () => {
      live = false
    }
  }, [shaderId])

  useEffect(() => {
    const next = startingValues(sliders, card.tunedValues)

    for (const name of Object.keys(next)) {
      if (Number.isFinite(valuesRef.current[name])) {
        next[name] = valuesRef.current[name]
      }
    }

    tuning.replace(next, false)
  }, [signature])

  useEffect(() => {
    if (!source || !canvasRef.current) {
      return
    }

    let lastWrite = 0
    const onTime = seconds => {
      const now = performance.now()

      if (timeRef.current && now - lastWrite > 80) {
        lastWrite = now
        timeRef.current.textContent = formatTime(seconds)
      }
    }
    const onState = action => {
      if (action.phase !== 'live') {
        const control = controlRef.current
        setScrub(control ? control.time() : 1)

        if (timeRef.current && control) {
          timeRef.current.textContent = formatTime(control.time())
        }
      }

      dispatch(action)
    }
    const control = createShaderPlayer(canvasRef.current, { source, sliders, values: valuesRef.current, onState, onTime })
    controlRef.current = control
    setScrub(control.time())

    return () => {
      controlRef.current = null
      control.stop()
    }
  }, [source, signature, reload])

  const closeMenu = refocus => {
    refocusRef.current = refocus
    setMenuOpen(false)
  }

  const setSlider = tuning.set
  const resetAll = tuning.resetAll

  const insert = async (text, done, failed) => {
    let inserted = false

    try {
      inserted = await sdk.host.composer.insertText(sessionId || null, text, { mode: 'block' })
    } catch {
      inserted = false
    }

    if (inserted) {
      say(done, 'ok')
    } else {
      await sdk.writeClipboardText(text)
      say(failed, 'warn')
    }

    return inserted
  }

  const askToFixExport = (row, problem) => {
    closeMenu(false)

    return insert(
      exportFixRequest(title, shaderId, version, row, problem),
      'Request put in the composer. Send it when ready.',
      'The composer did not take it, so the request is on the clipboard.'
    )
  }

  const askToFix = () => {
    const finding = state.findings && state.findings[0]
    const problem = finding ? `line ${finding.line}: ${finding.message}` : (state.log || 'it does not compile on my GPU').split('\n')[0]

    return insert(
      `The shader "${title}"${shaderId ? ` (id ${shaderId})` : ''} does not run in Desktop: ${problem}. Fix it and render it again.`,
      'Request put in the composer. Send it when ready.',
      'The composer did not take it, so the request is on the clipboard.'
    )
  }

  const exportNow = async row => {
    const name = targetName(row, true)
    setBusy(row.id)
    say(`Exporting ${name} and proving it. This can take a minute.`, 'none')

    try {
      const payload = await apiCall(`${shaderPath(shaderId)}/export/${encodeURIComponent(row.id)}${versionQuery}`, { method: 'POST', body: {}, timeoutMs: 180000 })
      await loadTargets()

      if (payload && payload.success === false) {
        say(`${name} export failed: ${payload.error || 'no reason given'}.`, 'err', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `failed: ${payload.error || 'no reason given'}`) }])
      } else {
        const verdict = payload && payload.proof ? payload.proof.verdict : 'not_proven'
        const why = payload && payload.proof && payload.proof.why ? ` (${payload.proof.why})` : ''

        if (verdict === 'match') {
          say(`${name} exported and proven: match.`, 'ok')
        } else if (verdict === 'differs') {
          say(`${name} exported, but the proof differs${why}.`, 'warn', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `has a proof that differs${why}`) }])
        } else {
          say(`${name} exported, not proven${why}.`, 'warn', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `is not proven${why}`) }])
        }
      }
    } catch (error) {
      const detail = restProblem(error).message
      say(`${name} export failed: ${detail || 'the plugin API did not answer'}.`, 'err', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `failed: ${detail || 'the plugin API did not answer'}`) }])
    } finally {
      setBusy('')
    }
  }

  /** The text of a target's file; a missing export becomes an error toast with a next step. */
  const fileText = async row => {
    const name = targetName(row, true)

    try {
      return await apiCall(`${shaderPath(shaderId)}/export/${encodeURIComponent(row.id)}${versionQuery}`)
    } catch (error) {
      const { status, message: detail } = restProblem(error)

      if (status === 409) {
        say(`${name} isn't exported yet.`, 'warn', [{ label: 'Export now', run: () => exportNow(row) }])
      } else {
        say(`Could not read ${name}: ${detail || 'the plugin API did not answer'}.`, 'err')
      }

      return null
    }
  }

  const copyFile = async row => {
    closeMenu(true)
    const payload = await fileText(row)

    if (payload && typeof payload.text === 'string') {
      await sdk.writeClipboardText(payload.text)
      say(`${payload.main_file || targetName(row, true)} copied`, 'ok')
    }
  }

  const sendToChat = async row => {
    closeMenu(false)
    const payload = await fileText(row)

    if (payload && typeof payload.text === 'string') {
      await insert(payload.text, `${payload.main_file || targetName(row, true)} put in the composer`,
        'The composer did not take it, so the file is on the clipboard.')
    }
  }

  const findings = useMemo(() => (source ? lintSource(source, sliders.map(slider => slider.name)) : []), [source, sliders])

  if (state.phase === 'unavailable' || state.phase === 'nowebgl') {
    return fallback
  }

  const phase = state.phase
  const playing = phase === 'live'
  const lost = phase === 'lost'
  const problem = phase === 'lint' || phase === 'error'
  const paused = phase === 'paused' || phase === 'still'
  const stateWord = phase === 'paused' && state.reason === 'slow' ? 'Paused: slow frames' : STATE_WORDS[phase] || 'Loading'
  const tunedAny = sliders.some(slider => Math.abs((values[slider.name] ?? slider.default) - slider.default) > 1e-9)
  const scrubMax = Math.max(10, Math.ceil((scrub + 5) / 5) * 5)

  const header = jsxs('header', {
    className: 'sb-head',
    children: [
      jsx('button', {
        type: 'button',
        className: 'sb-icon-btn',
        'aria-pressed': playing ? 'true' : 'false',
        'aria-label': playing ? 'Pause' : 'Play',
        title: playing ? 'Pause (Space on the canvas)' : 'Play (Space on the canvas)',
        disabled: problem || lost || phase === 'loading',
        onClick: () => controlRef.current?.toggle(),
        children: icon(playing ? 'debug-pause' : 'play')
      }),
      jsx('span', { className: 'sb-name', title: rawTitle, children: title }),
      version ? jsx('span', { className: 'sb-ver', title: `version ${version}`, children: `v${version}` }) : null,
      jsxs('span', { className: 'sb-state', role: 'status', children: [jsx('span', { className: 'sb-dot' }), stateWord] }),
      jsx('span', { className: 'sb-time', ref: timeRef, 'aria-label': 'Time', hidden: problem, children: formatTime(scrub) })
    ]
  })

  const problemPanel = problem
    ? jsxs('div', {
        className: 'sb-problem',
        role: 'alert',
        children: [
          jsxs('h4', {
            children: [icon('error', 'sb-err'), phase !== 'lint' ? 'This GPU could not compile the shader'
              : (state.findings || []).every(f => STALL_RULES.has(f.rule)) ? 'Not compiled: this shader would stall the GPU'
              : 'Not compiled: the GPU would reject this shader']
          }),
          jsx('p', {
            children: phase === 'lint'
              ? 'The local lint stopped it before compiling. The lines are marked in Show code.'
              : 'It compiled on the Hermes host, so this is a difference between GPUs. The log is below.'
          }),
          jsx('pre', {
            children: phase === 'lint'
              ? (state.findings || []).map(f => `Line ${f.line}: ${f.message}. ${capitalize(f.hint)}.`).join('\n')
              : state.log
          }),
          jsx('div', {
            children: jsx('button', { type: 'button', className: 'sb-btn', onClick: askToFix, children: [icon('comment'), 'Ask Hermes to fix it'] })
          })
        ]
      })
    : lost
      ? jsxs('div', {
          className: 'sb-problem',
          role: 'alert',
          children: [
            jsxs('h4', { children: [icon('warning', 'sb-warn'), 'The GPU dropped this preview'] }),
            jsx('p', { children: 'The browser took the WebGL context away, for example after a driver reset or with many previews open.' }),
            jsx('div', { children: jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'reload-preview', onClick: () => setReload(n => n + 1), children: [icon('refresh'), 'Reload preview'] }) })
          ]
        })
      : null

  const stage = jsxs('div', {
    className: 'sb-stage',
    style: problem || lost ? { display: 'none' } : undefined,
    children: [
      jsx('canvas', {
        // stop() loses the old player's context and Chromium hands a lost context back for the
        // same element, so every rebuild draws on a fresh canvas.
        key: `${textHash(source)}|${signature}|${reload}`,
        ref: canvasRef,
        className: 'sb-canvas',
        tabIndex: problem || lost ? -1 : 0,
        role: 'img',
        'aria-label': `${title}, shader preview. Press Space to ${playing ? 'pause' : 'play'}.`,
        onClick: () => controlRef.current?.toggle(),
        onKeyDown: event => {
          if (event.key === ' ' || event.key === 'Enter') {
            event.preventDefault()
            controlRef.current?.toggle()
          }
        }
      }),
      jsx('div', { className: 'sb-hint', 'aria-hidden': 'true', children: playing ? 'Click or press Space to pause' : 'Click or press Space to play' }),
      paused
        ? jsxs('div', {
            className: 'sb-scrub',
            children: [
              jsx('input', {
                type: 'range',
                min: 0,
                max: scrubMax,
                step: 0.01,
                value: scrub,
                'aria-label': 'Time in seconds',
                'aria-valuetext': `${scrub.toFixed(2)} seconds`,
                onChange: event => {
                  const t = Number(event.target.value)
                  setScrub(t)
                  controlRef.current?.seek(t)

                  if (timeRef.current) {
                    timeRef.current.textContent = formatTime(t)
                  }
                }
              }),
              jsx('span', { 'aria-hidden': 'true', children: `${scrub.toFixed(2)} s` })
            ]
          })
        : null
    ]
  })

  const sliderSection = sliders.length
    ? jsxs('section', {
        className: 'sb-section',
        'aria-label': 'Sliders',
        children: [
          jsxs('div', {
            className: 'sb-sec-head',
            children: [
              jsx('span', { className: 'sb-sec-title', children: 'Sliders' }),
              tunedAny ? jsx('button', { type: 'button', className: 'sb-link', onClick: resetAll, children: 'Reset all' }) : null
            ]
          }),
          ...sliders.map(slider => jsx(SliderRow, {
            slider,
            value: values[slider.name] ?? slider.default,
            onChange: (name, value) => setSlider(name, value, false),
            onBlur: tuning.flush,
            onReset: () => setSlider(slider.name, slider.default, true)
          }, slider.name))
        ]
      })
    : null

  // One chip per exported target, then one chip that names every target not exported yet, so the
  // strip stays on one line and its one action (export) reads once.
  const missing = targets.filter(row => !available(row))
  const chipTitle = row => [row.label, row.status === 'experimental' ? 'experimental' : '', row.proof && row.proof.why].filter(Boolean).join('. ')
  const proofStrip = shaderId && targets.length
    ? jsxs('div', {
        className: 'sb-proof',
        role: 'list',
        'aria-label': 'Export proof',
        children: [
          ...targets.filter(available).map(row => {
            const verdict = verdictOf(row)

            return jsxs('span', {
              className: 'sb-chip',
              role: 'listitem',
              'data-target': row.id,
              'data-verdict': verdict.word,
              title: chipTitle(row),
              children: [icon(verdict.icon, `sb-${verdict.tone}`), targetName(row, false), jsx('span', { className: `sb-${verdict.tone}`, children: verdict.word })]
            }, row.id)
          }),
          missing.length
            ? jsxs('span', {
                className: 'sb-chip',
                role: 'listitem',
                children: [
                  icon('circle-large-outline', 'sb-none'),
                  jsx('span', {
                    children: missing.flatMap((row, i) => [
                      i ? ', ' : null,
                      jsx('span', { 'data-target': row.id, 'data-verdict': 'not exported', title: chipTitle(row), children: targetName(row, false) }, row.id)
                    ])
                  }),
                  jsx('span', { className: 'sb-none', children: 'not exported' })
                ]
              }, 'missing')
            : null
        ]
      })
    : null

  const toastRow = toast
    ? jsxs('span', {
        className: 'sb-toast',
        children: [
          icon(toast.tone === 'ok' ? 'check' : toast.tone === 'err' ? 'error' : toast.tone === 'warn' ? 'warning' : 'info', `sb-${toast.tone}`),
          jsx('span', { className: 'sb-toast-text', children: toast.text }),
          ...(toast.actions || []).map(action => jsx('button', {
            type: 'button',
            className: 'sb-btn',
            onClick: () => {
              setToast(null)
              action.run()
            },
            children: action.label
          }, action.label))
        ]
      })
    : null

  const actions = shaderId
    ? jsxs('footer', {
        className: `sb-actions${proofStrip ? '' : ' sb-actions-only'}`,
        children: [
          jsx(ExportPopover, {
            id: menuId,
            open: menuOpen,
            onOpenChange: setMenuOpen,
            refocusRef,
            trigger: jsx('button', {
              type: 'button',
              className: 'sb-btn sb-primary',
              'aria-controls': menuId,
              disabled: !targets.length,
              title: targets.length ? undefined : 'The plugin API did not list any targets',
              children: ['Export', icon('chevron-down')]
            }),
            children: jsx(ExportMenu, { targets, busy, onCopy: copyFile, onSend: sendToChat, onExport: exportNow })
          }),
          jsx('button', {
            type: 'button',
            className: 'sb-btn sb-ghost',
            'aria-expanded': codeOpen ? 'true' : 'false',
            onClick: () => setCodeOpen(open => !open),
            children: [icon('code'), codeOpen ? 'Hide code' : 'Show code']
          }),
          jsx('span', { className: 'sb-sr', role: 'status', 'aria-live': 'polite', children: toast ? toast.text : '' }),
          toastRow
        ]
      })
    : jsxs('footer', {
        className: 'sb-actions sb-actions-only',
        children: [
          jsx('button', {
            type: 'button',
            className: 'sb-btn sb-ghost',
            'aria-expanded': codeOpen ? 'true' : 'false',
            onClick: () => setCodeOpen(open => !open),
            children: [icon('code'), codeOpen ? 'Hide code' : 'Show code']
          }),
          toastRow
        ]
      })

  return jsxs('figure', {
    className: 'sb-card',
    'data-shaderbruecke': phase,
    'aria-label': `${title} shader card`,
    children: [
      header,
      problemPanel,
      stage,
      sliderSection,
      proofStrip,
      actions,
      codeOpen ? jsx(CodeView, { source: source || argsSource, findings }) : null
    ]
  })
}

// ---------------------------------------------------------------------------------------------
// v0.2 library: the Shaders page (route, sidebar row, palette rows) and its detail sheet.
// Data comes from the plugin's library routes; images arrive as data URLs inside the JSON.
// Surfaces an older Desktop may lack (ctx.storage, ctx.files, host.openWorkspace) are
// feature-detected and fall back to memory, the clipboard and an in-page tab.
// ---------------------------------------------------------------------------------------------

const PAGE_PATH = '/shaderbruecke'
const QK = 'shaderbruecke'
const LONG_MS = 180000
const HOVER_DELAY_MS = 140
// sb:starter-prompts:begin
const STARTER_PROMPTS = [
  "Make a shader of fireflies drifting over a dark pond, with sliders for swarm size and glow.",
  "Make a shader of a slow ocean swell rolling in at dusk, with sliders for wave height and water colour.",
  "Make a shader of an aurora curtain swaying over snowy hills, with sliders for ribbon width and colour shift.",
  "Make a shader of lava lamp blobs rising and merging, with sliders for blob count and warmth.",
  "Make a shader of a desert dune field shifting in the wind, with sliders for dune height and sand colour.",
  "Make a shader of koi fish circling in a garden pool, with sliders for fish count and ripple strength.",
  "Make a shader of a nebula slowly churning in deep space, with sliders for dust density and hue.",
  "Make a shader of rain sliding down a window at night, with sliders for drop size and city glow.",
  "Make a shader of a candle flame swaying in a draught, with sliders for flame height and warmth.",
  "Make a shader of jellyfish pulsing through a dark sea, with sliders for jellyfish count and glow.",
  "Make a shader of clouds drifting over a mountain ridge, with sliders for cloud cover and wind speed.",
  "Make a shader of a kaleidoscope of stained glass turning slowly, with sliders for segment count and saturation.",
  "Make a shader of a field of wheat waving in a breeze, with sliders for wind strength and golden tint.",
  "Make a shader of ink drops blooming in water, with sliders for spread speed and ink colour.",
  "Make a shader of a starfield streaming past a spaceship, with sliders for star density and speed.",
  "Make a shader of a coral reef with swaying sea fans, with sliders for current strength and colour richness.",
  "Make a shader of smoke curling up from incense, with sliders for curl amount and smoke thickness.",
  "Make a shader of a sunrise over a misty lake, with sliders for mist density and sun height.",
  "Make a shader of snow falling over a pine forest, with sliders for snowfall rate and flake size.",
  "Make a shader of a spinning vinyl record with soft reflections, with sliders for spin speed and groove depth.",
  "Make a shader of bubbles rising in a glass of soda, with sliders for bubble count and fizz speed.",
  "Make a shader of a tide pool with sunlight caustics, with sliders for caustic scale and water depth.",
  "Make a shader of a paper lantern festival floating upward, with sliders for lantern count and drift.",
  "Make a shader of a slow plasma wave in pastel colours, with sliders for wave scale and softness.",
  "Make a shader of leaves tumbling across an autumn path, with sliders for leaf count and wind gusts.",
  "Make a shader of a whirlpool turning in a dark river, with sliders for swirl strength and foam.",
  "Make a shader of a glowing jungle floor with drifting spores, with sliders for spore density and glow.",
  "Make a shader of a lighthouse beam sweeping through fog, with sliders for beam width and fog density.",
  "Make a shader of a honeycomb pattern breathing in and out, with sliders for cell size and amber tint.",
  "Make a shader of a moonlit sea with a silver path, with sliders for moon size and wave calm.",
  "Make a shader of a sandstorm rolling over a canyon, with sliders for dust amount and sky colour.",
  "Make a shader of marbled paper slowly swirling, with sliders for swirl detail and palette shift.",
  "Make a shader of a cosy fireplace with drifting embers, with sliders for ember count and fire height.",
  "Make a shader of a field of tulips swaying in spring, with sliders for flower density and breeze.",
  "Make a shader of a slow-turning galaxy spiral, with sliders for arm count and core brightness.",
  "Make a shader of waves of silk folding over each other, with sliders for fold depth and sheen.",
  "Make a shader of a quiet snow globe being gently shaken, with sliders for flake count and settle speed.",
  "Make a shader of a rainforest waterfall with rising mist, with sliders for water flow and mist amount.",
  "Make a shader of a topographic map with flowing contour lines, with sliders for line spacing and flow speed.",
  "Make a shader of glowing plankton swirling behind a boat, with sliders for wake length and glow colour.",
  "Make a shader of a sunset over rolling hills, with sliders for hill height and sky warmth.",
  "Make a shader of a mossy stream flowing over stones, with sliders for flow speed and stone size.",
  "Make a shader of a cloud of butterflies fluttering upward, with sliders for butterfly count and wing colour.",
  "Make a shader of a retro synthwave road scrolling to the horizon, with sliders for grid speed and sun size.",
  "Make a shader of molten gold pouring and cooling, with sliders for pour speed and heat glow.",
  "Make a shader of a misty bamboo forest swaying, with sliders for stalk density and mist.",
  "Make a shader of a slowly rotating crystal with soft refraction, with sliders for facet count and tint.",
  "Make a shader of dandelion seeds floating on the wind, with sliders for seed count and wind drift.",
  "Make a shader of ripples spreading from raindrops on a puddle, with sliders for rain rate and ripple size.",
  "Make a shader of a cityscape at night with drifting fog, with sliders for building height and fog density.",
  "Make a shader of a field of stars reflected in a still lake, with sliders for star density and water calm.",
  "Make a shader of a flowing river of liquid chrome, with sliders for flow speed and reflectivity.",
  "Make a shader of a slowly blooming mandala of petals, with sliders for petal count and bloom speed.",
  "Make a shader of a cave with glowing crystals and drifting dust, with sliders for crystal count and glow.",
  "Make a shader of seaweed swaying in a kelp forest, with sliders for current speed and kelp density.",
  "Make a shader of a warm desert mirage shimmering over sand, with sliders for heat haze and horizon height.",
  "Make a shader of soap film colours swirling on a bubble, with sliders for film thickness and swirl.",
  "Make a shader of a tea cup with steam curling upward, with sliders for steam amount and cup warmth.",
  "Make a shader of a slow meteor shower over a meadow, with sliders for meteor count and trail length.",
  "Make a shader of a rolling fog bank over a harbour, with sliders for fog height and drift speed.",
  "Make a shader of an underwater sun shaft scene, with sliders for ray count and water murk.",
  "Make a shader of a wind chime of glass shards turning slowly, with sliders for shard count and colour.",
  "Make a shader of a cherry blossom tree shedding petals, with sliders for petal rate and pink tint.",
  "Make a shader of a gentle hillside with grazing clouds of sheep, with sliders for flock size and grass colour.",
  "Make a shader of a woven tapestry rippling in a breeze, with sliders for weave scale and ripple strength.",
  "Make a shader of an hourglass with sand pouring through, with sliders for pour rate and sand colour.",
  "Make a shader of a mountain lake with drifting ice floes, with sliders for floe count and water colour.",
  "Make a shader of planets orbiting a quiet star, with sliders for planet count and orbit speed.",
  "Make a shader of a glacier cracking and drifting blue ice, with sliders for crack density and ice tint.",
  "Make a shader of a gentle tide washing over pebbles, with sliders for tide reach and foam.",
  "Make a shader of a sky full of kites dancing on the wind, with sliders for kite count and wind strength.",
  "Make a shader of a slowly melting ice cream sundae, with sliders for melt speed and topping colour.",
  "Make a shader of floating islands drifting in the clouds, with sliders for island count and cloud height.",
  "Make a shader of a forest canopy with sun dapples moving, with sliders for leaf density and sun angle.",
  "Make a shader of origami cranes gliding in circles, with sliders for crane count and paper colour.",
  "Make a shader of a hot air balloon rising over valleys, with sliders for rise speed and valley haze.",
  "Make a shader of a slow sea of rolling hills made of velvet, with sliders for hill scale and sheen.",
  "Make a shader of an abstract flow field of drifting particles, with sliders for particle count and curl.",
  "Make a shader of a lily pond with dragonflies hovering, with sliders for lily count and water tint.",
  "Make a shader of a slow-rotating geode with sparkling crystals, with sliders for crystal size and hue.",
  "Make a shader of a tidal estuary with winding channels, with sliders for channel width and water level.",
  "Make a shader of chalk dust drifting through a sunbeam, with sliders for dust density and beam width.",
  "Make a shader of a calm harbour with boats bobbing, with sliders for boat count and wave size.",
  "Make a shader of a slow solar eclipse with a soft corona, with sliders for corona size and sky darkness.",
  "Make a shader of a drifting cloudscape seen from a plane, with sliders for cloud density and altitude.",
  "Make a shader of a garden of glowing mushrooms at night, with sliders for mushroom count and glow colour.",
  "Make a shader of an endless tunnel of soft rings, with sliders for ring spacing and travel speed.",
  "Make a shader of a swarm of starlings forming shapes, with sliders for flock size and turn rate.",
  "Make a shader of hot springs steaming in the snow, with sliders for steam amount and water colour.",
  "Make a shader of water flowing over a rocky weir, with sliders for flow rate and foam amount.",
  "Make a shader of a beach at low tide with wet sand reflections, with sliders for sky colour and sand shine.",
  "Make a shader of a breathing gradient of warm and cool tones, with sliders for breath speed and contrast.",
  "Make a shader of an octopus arm curling through coral, with sliders for curl speed and skin colour.",
  "Make a shader of a wheat-coloured prairie under drifting clouds, with sliders for cloud speed and grass height.",
  "Make a shader of glowing ribbons tying themselves in knots, with sliders for ribbon count and twist.",
  "Make a shader of a sleepy village under falling snow, with sliders for snowfall and window warmth.",
  "Make a shader of the surface of a sun with slow granules, with sliders for granule size and heat.",
  "Make a shader of a flock of paper boats drifting downstream, with sliders for boat count and current.",
  "Make a shader of a quiet volcano with a slow lava glow, with sliders for lava flow and smoke.",
  "Make a shader of a mosaic of tiles gently shifting colour, with sliders for tile size and colour speed."
]
// sb:starter-prompts:end

let lastPrompt = ''

/** A random starter prompt, never the one picked last. */
function pickPrompt() {
  const pool = STARTER_PROMPTS.filter(prompt => prompt !== lastPrompt)
  lastPrompt = pool[Math.floor(Math.random() * pool.length)]

  return lastPrompt
}

/** The `starter_prompt` setting, read fresh: Desktop's query cache would keep an old value for a minute. */
async function starterPromptOn() {
  try {
    const answer = await apiCall('/settings')

    return !(answer && answer.starter_prompt === false)
  } catch {
    return true
  }
}

let pluginStore = null
let pluginFiles = null
const memoryStore = new Map()

function storeGet(key, fallback) {
  if (pluginStore) {
    try {
      return pluginStore.get(key, fallback)
    } catch {
      // A broken store reads like an empty one.
    }
  }

  return memoryStore.has(key) ? memoryStore.get(key) : fallback
}

function storeSet(key, value) {
  if (pluginStore) {
    try {
      pluginStore.set(key, value)

      return
    } catch {
      // Fall back to memory below.
    }
  }

  memoryStore.set(key, value)
}

function storeRemove(key) {
  if (pluginStore) {
    try {
      pluginStore.remove(key)
    } catch {
      // Nothing to remove.
    }
  }

  memoryStore.delete(key)
}

/** A contract error (`{detail: {error, message, ...}}` inside a rejected ctx.rest) as fields. */
function restProblem(error) {
  const text = String(error && error.message ? error.message : error || '')
  const status = Number((text.match(/\b([45]\d\d):/) || [])[1]) || 0
  let detail = null

  try {
    const body = JSON.parse(text.slice(text.indexOf('{')))
    detail = body && body.detail !== undefined ? body.detail : body
  } catch {
    detail = null
  }

  const fields = detail && typeof detail === 'object' ? detail : {}
  const message = fields.message || (typeof detail === 'string' ? detail : text.replace(/^.*?\b[45]\d\d:\s*/, ''))

  return { status, code: fields.error || '', message, detail: fields }
}

function problemText(error) {
  const problem = restProblem(error)

  if (problem.status === 404 && /plugin not found/i.test(problem.message)) {
    return 'Shaderbrücke is not on in this profile. Open a chat of a profile that has it, then try again.'
  }

  return problem.message || 'The plugin API did not answer.'
}

/** Display names never carry the en or em dashes an agent may write; a hyphen replaces them. */
function displayName(name) {
  return String(name || 'Untitled shader').replace(/\s*[\u2013\u2014]\s*/g, ' - ')
}

function ago(iso) {
  const t = Date.parse(iso || '')

  if (!Number.isFinite(t)) {
    return ''
  }

  const s = Math.max(0, (Date.now() - t) / 1000)

  if (s < 60) {
    return 'just now'
  }

  if (s < 3600) {
    return `${Math.round(s / 60)} min ago`
  }

  if (s < 86400) {
    return `${Math.round(s / 3600)} h ago`
  }

  if (s < 7 * 86400) {
    const days = Math.round(s / 86400)

    return days === 1 ? 'yesterday' : `${days} days ago`
  }

  return new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}

function licenceLabel(spdx) {
  if (!spdx) {
    return 'Private'
  }

  if (/^CC0/.test(spdx)) {
    return 'CC0'
  }

  const match = /^CC-(.+)-(\d+\.\d+)$/.exec(spdx)

  return match ? `CC ${match[1]} ${match[2]}` : spdx
}

const AUTHOR_WORDS = { agent: 'Hermes', human: 'You', import: 'Imported' }

const STATE_INFO = {
  flat: { word: 'Flat frame', tone: 'warn', glyph: 'warning' },
  no_thumb: { word: 'No preview yet', tone: 'none', glyph: 'circle-slash' },
  frames_pruned: { word: 'Frames pruned', tone: 'none', glyph: 'history' }
}

const STATE_FILTERS = [
  ['', 'Any state'],
  ['ok', 'Checks ok'],
  ['flat', 'Flat frame'],
  ['flash_warn', 'May flash'],
  ['flash_block', 'Flash risk'],
  ['legacy', 'Not flash-checked'],
  ['no_thumb', 'No preview yet'],
  ['frames_pruned', 'Frames pruned']
]

/** Why an item (a row or a version) does not play on its own, or null when it may (constraint 7). */
function playGate(item) {
  if (!item || item.autoplay) {
    return null
  }

  const flash = item.flash !== undefined ? item.flash : item.checks ? item.checks.flash : null

  if (flash === 'block') {
    return { level: 'block', word: 'Flash risk', tone: 'err', glyph: 'warning',
      text: 'The flash check found flashing that can trigger seizures in some people. It never plays on its own.' }
  }

  if (flash === 'warn') {
    return { level: 'warn', word: 'May flash', tone: 'warn', glyph: 'warning',
      text: 'The flash check found some flashing, so it waits for your click.' }
  }

  if (flash === null || flash === undefined) {
    return { level: 'unchecked', word: 'Not flash-checked', tone: 'warn', glyph: 'shield',
      text: 'This version was made before the flash check, so it waits for your click.' }
  }

  return { level: 'unchecked', word: 'Click to play', tone: 'warn', glyph: 'play-circle',
    text: 'The server check did not pass for this version, so it waits for your click.' }
}

function Chip({ tone = 'none', glyph, title, children }) {
  return jsxs('span', {
    className: `sb-pchip sb-pchip-${tone}`,
    title,
    children: [glyph ? icon(glyph) : null, children]
  })
}

/** The state chips of a row or version: the play gate first, then the other states in words. */
function stateChips(item) {
  const gate = playGate(item)
  const states = (item.states || []).filter(state => STATE_INFO[state])
  const chips = []

  if (gate) {
    chips.push(jsx(Chip, { tone: gate.tone, glyph: gate.glyph, title: gate.text, children: gate.word }, 'gate'))
  }

  for (const state of states) {
    const info = STATE_INFO[state]
    chips.push(jsx(Chip, { tone: info.tone, glyph: info.glyph, children: info.word }, state))
  }

  if (!gate && !(item.states || []).length) {
    chips.push(jsx(Chip, { tone: 'ok', glyph: 'pass', children: 'Checks ok' }, 'ok'))
  }

  return chips
}

function licenceChip(row) {
  if (row.licenceInherited) {
    return jsx(Chip, { tone: 'acc', glyph: 'law', title: 'Inherited from the imported source; it stays with every version.',
      children: `${licenceLabel(row.licence || row.licenceInherited)}, inherited` }, 'licence')
  }

  return row.licence
    ? jsx(Chip, { tone: 'acc', glyph: 'law', children: licenceLabel(row.licence) }, 'licence')
    : jsx(Chip, { glyph: 'lock', children: 'Private' }, 'licence')
}

/** Every chip a tile shows, in reading order. Pruned frames are detail-level news, so tiles skip it. */
function rowChips(row) {
  return [
    jsx(Chip, { glyph: 'versions', children: row.versions === 1 ? '1 version' : `${row.versions} versions` }, 'count'),
    ...stateChips({ ...row, states: (row.states || []).filter(state => state !== 'frames_pruned') }),
    licenceChip(row),
    row.imported ? jsx(Chip, { glyph: 'cloud-download', title: row.imported.url || undefined, children: 'Imported' }, 'imported') : null,
    row.starred ? jsx(Chip, { tone: 'acc', glyph: 'star-full', children: 'Starred' }, 'starred') : null
  ]
}

function subLine(row) {
  const parts = [`Edited ${ago(row.updatedAt)}`]

  if (row.parent) {
    parts.push(`forked from ${displayName(row.parent.name)}`)
  }

  return parts.join(', ')
}

const toastSubscribers = new Set()

/** A short message in the page's or the Lab's status line (`scope`), with optional actions. */
function say(text, tone = 'none', actions = null, scope = 'page') {
  for (const listener of toastSubscribers) {
    listener({ text, tone, actions, scope })
  }
}

function Toaster({ scope }) {
  const [toast, setToast] = useState(null)

  useEffect(() => {
    let timer = 0
    const listener = next => {
      if (next.scope !== scope) {
        return
      }

      window.clearTimeout(timer)
      setToast(next)
      timer = window.setTimeout(() => setToast(null), next.actions ? ACTION_TOAST_MS : 6000)
    }

    toastSubscribers.add(listener)

    return () => {
      toastSubscribers.delete(listener)
      window.clearTimeout(timer)
    }
  }, [scope])

  return jsx('div', {
    className: 'sb-toaster',
    role: 'status',
    'aria-live': 'polite',
    children: toast
      ? jsxs('div', {
          className: 'sb-toast-box',
          'data-tone': toast.tone,
          children: [
            icon(toast.tone === 'ok' ? 'check' : toast.tone === 'err' ? 'error' : toast.tone === 'warn' ? 'warning' : 'info', `sb-${toast.tone}`),
            jsx('span', { className: 'sb-toast-text', children: toast.text }),
            ...(toast.actions || []).map(action => jsx('button', {
              type: 'button',
              className: 'sb-btn',
              onClick: () => {
                setToast(null)
                action.run()
              },
              children: action.label
            }, action.label)),
            jsx('button', { type: 'button', className: 'sb-icon-btn', 'aria-label': 'Dismiss', onClick: () => setToast(null), children: icon('close') })
          ]
        })
      : null
  })
}

async function copyText(text) {
  try {
    if (typeof sdk.writeClipboardText === 'function') {
      await sdk.writeClipboardText(text)

      return true
    }
  } catch {
    // Try the next door.
  }

  try {
    if (pluginApi && pluginApi.os && typeof pluginApi.os.writeClipboard === 'function') {
      return Boolean(await pluginApi.os.writeClipboard(text))
    }
  } catch {
    // Try the next door.
  }

  try {
    await navigator.clipboard.writeText(text)

    return true
  } catch {
    return false
  }
}

const sleep = ms => new Promise(resolve => window.setTimeout(resolve, ms))

async function setDraftConfirmed(composer, sessionId, text) {
  // The new tab mounts its composer a moment after openSession resolves; until then setDraft
  // answers false (no surface claimed it), so retry briefly.
  for (let attempt = 0; attempt < 14; attempt++) {
    let accepted = false

    try {
      accepted = (await composer.setDraft(sessionId, text)) === true
    } catch {
      accepted = false
    }

    if (accepted) {
      if (typeof composer.getDraft !== 'function') {
        return true
      }

      const back = await composer.getDraft(sessionId).catch(() => null)

      if (typeof back === 'string' && back.trim() === text.trim()) {
        return true
      }
    }

    await sleep(150)
  }

  return false
}

/**
 * Open one new chat tab with `text` drafted (never sent): session.create, title it, open it as a
 * tab, setDraft (must answer true), getDraft to confirm. Falls back to a blank chat, then to the
 * clipboard. The user adds a sentence and sends.
 */
async function draftInNewChat(text, title) {
  const host = sdk.host || {}
  const composer = host.composer || {}

  if (typeof host.request === 'function' && typeof host.openSession === 'function' && typeof composer.setDraft === 'function') {
    try {
      const created = await host.request('session.create', { title })
      const runtime = created && created.session_id
      const stored = (created && created.stored_session_id) || runtime

      if (runtime) {
        try {
          // session.create is lazy; titling materialises the stored row before the tab opens it.
          await host.request('session.title', { session_id: runtime, title })
        } catch {
          // Older gateways: the first prompt titles it.
        }

        await host.openSession(stored, { intent: 'tab' })

        if (!text) {
          return { ok: true, via: 'session' }
        }

        if (await setDraftConfirmed(composer, runtime, text)) {
          return { ok: true, via: 'session' }
        }
      }
    } catch {
      // Fall through to a blank chat.
    }
  }

  if (typeof host.newChat === 'function' && typeof composer.setDraft === 'function') {
    try {
      host.newChat()

      if (!text || (await setDraftConfirmed(composer, null, text))) {
        return { ok: true, via: 'newChat' }
      }
    } catch {
      // Fall through to the clipboard.
    }
  }

  return { ok: false, via: text && (await copyText(text)) ? 'clipboard' : 'none' }
}

function shaderRef(id, version) {
  return `@shader:${id}@v${version}\n`
}

async function continueInChat(row, version) {
  const n = version || row.version
  const text = shaderRef(row.id, n)
  say(`Opening a chat with ${displayName(row.name)} v${n}`, 'none')
  const result = await draftInNewChat(text, `Shader: ${displayName(row.name)}`)

  if (result.ok) {
    say(`${displayName(row.name)} v${n} is drafted in a new chat. Add what you want changed and send.`, 'ok')
  } else if (result.via === 'clipboard') {
    say(`Could not open a chat, so ${text.trim()} is on the clipboard. Paste it into any chat.`, 'warn')
  } else {
    say(`Could not open a chat. Type ${text.trim()} into any chat to continue.`, 'err')
  }

  return result
}

async function newShader(prompt) {
  const text = (await starterPromptOn()) ? prompt || pickPrompt() : ''
  const result = await draftInNewChat(text, 'New shader')

  if (result.ok && !text) {
    say('A new chat is open. Describe your shader and send.', 'ok')
  } else if (result.ok) {
    say('A new chat has a starter prompt. Describe your shader and send.', 'ok')
  } else if (result.via === 'clipboard') {
    say('Could not open a chat, so the starter prompt is on the clipboard.', 'warn')
  } else {
    say('Could not open a chat. Ask Hermes for a shader in any chat.', 'err')
  }

  return result
}

/** Save share files (`GET /shaders/{id}/share`) into a folder the user picks, else to the clipboard. */
async function saveShareFiles(items, scope = 'page') {
  let answers = []

  try {
    for (const item of items) {
      const query = item.version ? `?version=${encodeURIComponent(item.version)}` : ''
      answers.push(await apiCall(`${shaderPath(item.id)}/share${query}`))
    }
  } catch (error) {
    say(`Could not build the share file: ${problemText(error)}`, 'err', null, scope)

    return { ok: false }
  }

  answers = answers.filter(answer => answer && answer.share && answer.filename)

  if (pluginFiles) {
    const picked = await pluginFiles.requestFolder()

    if (picked && picked.ok && !picked.folder) {
      say('No folder picked, nothing saved.', 'none', null, scope)

      return { ok: false, cancelled: true }
    }

    if (picked && picked.ok && picked.folder) {
      const written = []

      for (const answer of answers) {
        const result = await pluginFiles.writeText(picked.folder.id, answer.filename, JSON.stringify(answer.share, null, 2) + '\n')

        if (result && result.ok) {
          written.push(answer.filename)
        }
      }

      if (written.length === answers.length) {
        say(`Saved ${written.length === 1 ? written[0] : `${written.length} share files`} to ${picked.folder.name}.`, 'ok', null, scope)
      } else {
        say(`Saved ${written.length} of ${answers.length} share files to ${picked.folder.name}.`, 'warn', null, scope)
      }

      return { ok: written.length > 0, written }
    }
  }

  const text = JSON.stringify(answers.length === 1 ? answers[0].share : answers.map(answer => answer.share), null, 2)
  const copied = await copyText(text)
  const name = answers.length === 1 ? answers[0].filename : 'one file per shader'
  say(copied
    ? `This Desktop cannot save plugin files here, so the share JSON is on the clipboard. Save it as ${name}.`
    : 'Could not save or copy the share file.', copied ? 'warn' : 'err', null, scope)

  return { ok: copied, clipboard: true }
}

/** Read the page's view state from the hash query (`#/shaderbruecke?shader=...`). */
function readRouteQuery() {
  const hash = typeof window === 'undefined' ? '' : window.location.hash || ''
  const index = hash.indexOf('?')

  return index < 0 ? '' : hash.slice(index + 1)
}

function useRouteQuery() {
  const [query, setQuery] = useState(readRouteQuery)

  useEffect(() => {
    const onChange = () => setQuery(readRouteQuery())
    window.addEventListener('hashchange', onChange)

    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  return useMemo(() => new URLSearchParams(query), [query])
}

function goPage(params) {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') {
      search.set(key, String(value))
    }
  }

  const qs = search.toString()
  const path = qs ? `${PAGE_PATH}?${qs}` : PAGE_PATH

  if (sdk.host && typeof sdk.host.navigate === 'function') {
    sdk.host.navigate(path)
  } else {
    window.location.hash = `#${path}`
  }
}

function patchPage(query, patch) {
  goPage({ ...Object.fromEntries(query), ...patch })
}

const qkey = (...parts) => [QK, ...parts]

const LIBRARY_POLL_MS = 2000

function libraryUrl(qs, cursor) {
  const query = [qs, cursor ? `cursor=${encodeURIComponent(cursor)}` : ''].filter(Boolean).join('&')

  return `/library${query ? `?${query}` : ''}`
}

/** `data` with the next page appended. The cursor is an offset, so a row that moved up a page
 * since the first one loaded can come again; it is shown once. */
function mergePage(data, next) {
  const seen = new Set(data.rows.map(row => row.id))

  return { ...data, rows: [...data.rows, ...((next && next.rows) || []).filter(row => !seen.has(row.id))], cursor: (next && next.cursor) || null, pages: (data.pages || 1) + 1 }
}

/**
 * One library query (filters in `qs`) with every page 'Load more' added, kept as one cache entry:
 * a refetch after a change asks for as many pages again from the first, so later pages never go
 * stale against page one. (The SDK exports no useInfiniteQuery.)
 */
function useLibrary(qs) {
  const client = sdk.useQueryClient()

  return sdk.useQuery({
    queryKey: qkey('library', qs),
    queryFn: async ({ queryKey }) => {
      const cached = client.getQueryData(queryKey)
      const wanted = (cached && cached.pages) || 1
      let data = { ...(await apiCall(libraryUrl(qs))), pages: 1 }

      while (data.pages < wanted && data.cursor) {
        data = mergePage(data, await apiCall(libraryUrl(qs, data.cursor)))
      }

      return data
    },
    placeholderData: previous => previous,
    staleTime: 5000,
    // While a slider save re-renders a thumb, poll until the job is gone and the new stamp is in.
    refetchInterval: query => {
      const data = query && query.state ? query.state.data : query

      return data && (data.jobs || []).some(job => job.kind === 'thumb_refresh' && JOB_ACTIVE.has(job.state)) ? LIBRARY_POLL_MS : false
    }
  })
}

function useVersionDoc(id, version, enabled = true) {
  return sdk.useQuery({
    queryKey: qkey('version', id || '', Number(version) || 0),
    queryFn: () => apiCall(`${shaderPath(id)}/versions/${encodeURIComponent(version)}`),
    enabled: Boolean(enabled && id && version),
    staleTime: 60000
  })
}

const JOB_ACTIVE = new Set(['queued', 'running'])

function useJob(id) {
  return sdk.useQuery({
    queryKey: qkey('job', id || ''),
    queryFn: () => apiCall(`/jobs/${encodeURIComponent(id)}`).then(answer => (answer && answer.job ? answer.job : null)),
    enabled: Boolean(id),
    // React Query 5 passes the query, 4 passed the data first; read either.
    refetchInterval: query => {
      const job = query && query.state ? query.state.data : query

      return job && JOB_ACTIVE.has(job.state) ? 1200 : false
    }
  })
}

const THUMB_BATCH = 60

const THUMB_CACHE_MAX = 600

/** A thumb to fetch: `<id>@v<n>` plus the file's mtime (thumbStamp), which changes when a slider
 * save re-renders it; null when there is nothing to fetch. */
function thumbItem(id, version, stamp) {
  return id && version ? { key: `${id}@v${version}`, entry: `${id}@v${version}|${stamp === null || stamp === undefined ? '' : stamp}` } : null
}

function rowThumbItem(row) {
  return row && !row.thumb ? thumbItem(row.id, row.thumbVersion, row.thumbStamp) : null
}

// Thumbs by `key|stamp` for the whole page session, outside React Query: entries are immutable
// per stamp, so they never need a refetch, and no observer-less cache entry can expire into a
// refetch storm. Oldest entries go past THUMB_CACHE_MAX.
const thumbCache = new Map()
const thumbLoading = new Set()
const thumbListeners = new Set()

function keepThumb(entry, value) {
  thumbCache.delete(entry)
  thumbCache.set(entry, value)

  while (thumbCache.size > THUMB_CACHE_MAX) {
    thumbCache.delete(thumbCache.keys().next().value)
  }
}

async function loadThumbs(items) {
  for (let i = 0; i < items.length; i += THUMB_BATCH) {
    const chunk = items.slice(i, i + THUMB_BATCH)
    let thumbs = {}

    try {
      const answer = await apiCall(`/thumbs?ids=${chunk.map(item => encodeURIComponent(item.key)).join(',')}`)
      thumbs = (answer && answer.thumbs) || {}
    } catch {
      // A failed batch reads as 'No preview yet'; a later stamp asks again.
    }

    for (const item of chunk) {
      thumbLoading.delete(item.entry)
      keepThumb(item.entry, typeof thumbs[item.key] === 'string' ? thumbs[item.key] : null)
    }

    for (const listener of thumbListeners) {
      listener()
    }
  }
}

/**
 * Thumbs for `items` (thumbItem), fetched in batched GET /thumbs calls of at most 60 keys.
 * Returns key -> data URL | null (no image) | undefined (loading).
 */
function useThumbs(items) {
  const [, bump] = useReducer(n => n + 1, 0)
  const wanted = []
  const seen = new Set()

  for (const item of items) {
    if (item && !seen.has(item.entry)) {
      seen.add(item.entry)
      wanted.push(item)
    }
  }

  const missing = wanted.filter(item => !thumbCache.has(item.entry) && !thumbLoading.has(item.entry))
  const missingKey = missing.map(item => item.entry).join(',')

  useEffect(() => {
    thumbListeners.add(bump)

    return () => thumbListeners.delete(bump)
  }, [])

  useEffect(() => {
    const todo = missing.filter(item => !thumbCache.has(item.entry) && !thumbLoading.has(item.entry))

    if (todo.length) {
      todo.forEach(item => thumbLoading.add(item.entry))
      loadThumbs(todo)
    }
  }, [missingKey])

  const map = {}

  for (const item of wanted) {
    map[item.key] = thumbCache.get(item.entry)
  }

  return map
}

/** The image of a row: inline, from the batch, null when it has none, undefined while loading. */
function thumbOf(row, thumbs) {
  if (row.thumb) {
    return row.thumb
  }

  const item = rowThumbItem(row)

  return item ? thumbs[item.key] : null
}

/** After a change: the library pages and, with an `id`, that shader's detail. Version documents
 * never change, so they stay. */
function invalidateShader(client, id) {
  if (!client) {
    return Promise.resolve()
  }

  return Promise.all([
    client.invalidateQueries({ queryKey: qkey('library') }),
    id ? client.invalidateQueries({ queryKey: qkey('detail', id) }) : null
  ])
}

/** A canvas driven by the one WebGL2 player (createShaderPlayer); React only mounts and unmounts it. */
function LivePlayer({ source, sliders, values, autoplay = true, reload = 0, className, label, hidden, controlRef, onPhase, onTime }) {
  const canvasRef = useRef(null)
  const valuesRef = useRef(values)
  const callbacks = useRef({ onPhase, onTime })
  const list = sliders || []
  const signature = sliderSignature(list)
  valuesRef.current = values
  callbacks.current = { onPhase, onTime }

  useEffect(() => {
    if (!source || !canvasRef.current) {
      return undefined
    }

    const control = createShaderPlayer(canvasRef.current, {
      source,
      sliders: list,
      values: valuesRef.current,
      autoplay,
      onState: state => callbacks.current.onPhase && callbacks.current.onPhase(state),
      onTime: seconds => callbacks.current.onTime && callbacks.current.onTime(seconds)
    })

    if (controlRef) {
      controlRef.current = control
    }

    return () => {
      if (controlRef && controlRef.current === control) {
        controlRef.current = null
      }

      control.stop()
    }
  }, [source, signature, autoplay, reload])

  return jsx('canvas', {
    // stop() loses the old player's context and Chromium hands that lost context back for the
    // same element, so every rebuild gets a fresh canvas.
    key: `${textHash(source)}|${signature}|${autoplay}|${reload}`,
    ref: canvasRef,
    className,
    role: hidden ? undefined : 'img',
    'aria-hidden': hidden ? 'true' : undefined,
    'aria-label': hidden ? undefined : label
  })
}

/** The one live canvas of the grid: mounted only on the hovered or focused tile, and only when it may autoplay. */
function TileLive({ row }) {
  const doc = useVersionDoc(row.id, row.version).data

  if (!doc || typeof doc.source !== 'string') {
    return null
  }

  return jsx(LivePlayer, {
    source: doc.source,
    sliders: doc.sliders || [],
    values: doc.effectiveValues || doc.tunedValues || null,
    autoplay: true,
    hidden: true,
    className: 'sb-tile-live'
  })
}

function Thumb({ row, image, live }) {
  const src = image

  return jsxs('div', {
    className: 'sb-thumb',
    'data-live': live ? 'true' : undefined,
    'data-thumb': src ? 'image' : src === null ? 'none' : 'loading',
    children: [
      src
        ? jsx('img', { src, alt: '', draggable: false })
        : src === null
          ? jsx('div', { className: 'sb-nothumb', children: jsx('span', { children: 'No preview yet' }) })
          : null,
      live ? jsx(TileLive, { row }) : null
    ]
  })
}

function SelectBox({ row, selected, onSelect }) {
  return jsx('label', {
    className: 'sb-sel',
    onClick: event => event.stopPropagation(),
    children: jsx('input', {
      type: 'checkbox',
      tabIndex: -1,
      checked: selected,
      'aria-label': `Select ${displayName(row.name)}`,
      onChange: event => onSelect(row.id, Boolean(event.nativeEvent && event.nativeEvent.shiftKey))
    })
  })
}

function tileLabel(row, selected) {
  const gate = playGate(row)

  return `${displayName(row.name)}, version ${row.version} of ${row.versions}${gate ? `, ${gate.word}` : ''}${selected ? ', selected' : ''}. Enter opens it, Space selects it.`
}

function TileView({ row, image, active, open, selected, live, onOpen, onSelect, onHover }) {
  return jsxs('div', {
    className: 'sb-tile',
    role: 'listitem',
    tabIndex: active ? 0 : -1,
    'data-tile-id': row.id,
    'data-autoplay': row.autoplay ? 'true' : 'false',
    'data-open': open ? 'true' : undefined,
    'aria-label': tileLabel(row, selected),
    onClick: () => onOpen(row.id),
    onMouseEnter: () => onHover(row.id),
    onMouseLeave: () => onHover(null),
    onFocus: () => onHover(row.id),
    onBlur: () => onHover(null),
    children: [
      jsxs('div', { className: 'sb-thumb-wrap', children: [jsx(Thumb, { row, image, live }), jsx(SelectBox, { row, selected, onSelect })] }),
      jsxs('div', { className: 'sb-tname', children: [jsx('b', { title: displayName(row.name), children: displayName(row.name) }), jsx('span', { className: 'sb-ver', children: `v${row.version}` })] }),
      jsx('div', { className: 'sb-tsub', children: subLine(row) }),
      jsx('div', { className: 'sb-chips', children: rowChips(row) })
    ]
  })
}

function ListRowView({ row, image, active, open, selected, live, onOpen, onSelect, onHover }) {
  return jsxs('div', {
    className: 'sb-lrow',
    role: 'listitem',
    tabIndex: active ? 0 : -1,
    'data-tile-id': row.id,
    'data-autoplay': row.autoplay ? 'true' : 'false',
    'data-open': open ? 'true' : undefined,
    'aria-label': tileLabel(row, selected),
    onClick: () => onOpen(row.id),
    onMouseEnter: () => onHover(row.id),
    onMouseLeave: () => onHover(null),
    onFocus: () => onHover(row.id),
    onBlur: () => onHover(null),
    children: [
      jsx(SelectBox, { row, selected, onSelect }),
      jsx(Thumb, { row, image, live }),
      jsxs('div', {
        style: { minWidth: 0 },
        children: [
          jsxs('div', { className: 'sb-tname', style: { marginTop: 0 }, children: [jsx('b', { children: displayName(row.name) }), jsx('span', { className: 'sb-ver', children: `v${row.version}` })] }),
          jsx('div', { className: 'sb-tsub', children: subLine(row) })
        ]
      }),
      jsx('div', { className: 'sb-chips', children: rowChips(row) })
    ]
  })
}

// A hover changes the live tile only; memo keeps the other tiles from rendering again.
const Tile = memo(TileView)
const ListRow = memo(ListRowView)

function columnsOf(nodes) {
  if (!nodes.length) {
    return 1
  }

  const top = nodes[0].offsetTop

  return Math.max(1, nodes.filter(node => node.offsetTop === top).length)
}

function JobLine({ job, verb }) {
  if (!job) {
    return null
  }

  return jsxs('span', {
    className: 'sb-jobline',
    children: [icon('loading', 'codicon-modifier-spin'), ` ${verb} ${job.done} of ${job.total}`]
  })
}

function AdoptBanner({ adoptable, jobId, onStart, onDismiss, onDone }) {
  const job = useJob(jobId).data
  const doneRef = useRef(false)

  useEffect(() => {
    if (job && job.state === 'done' && !doneRef.current) {
      doneRef.current = true
      onDone()
    }
  }, [job && job.state])

  if (job && JOB_ACTIVE.has(job.state)) {
    return jsxs('div', {
      className: 'sb-banner',
      'data-banner': 'adopt',
      children: [icon('inbox'), jsxs('div', { children: [jsx('b', { children: 'Adding older shaders' }), jsx(JobLine, { job, verb: 'Rendered' })] })]
    })
  }

  if (job && (job.state === 'done' || job.state === 'failed' || job.state === 'stale')) {
    const adopted = (job.result && job.result.adopted) || []
    const skipped = (job.result && job.result.skipped) || (job.errors || []).map(error => ({ folder: error.item, reason: error.reason }))

    return jsxs('div', {
      className: 'sb-banner',
      'data-banner': 'adopt-result',
      children: [
        icon(job.state === 'done' ? 'check' : 'warning', job.state === 'done' ? 'sb-ok' : 'sb-warn'),
        jsxs('div', {
          children: [
            jsx('b', { children: job.state === 'done'
              ? `Added ${adopted.length} older ${adopted.length === 1 ? 'shader' : 'shaders'}`
              : `Adding stopped after ${job.done} of ${job.total}` }),
            skipped.length
              ? jsxs('p', { children: [`${skipped.length} ${skipped.length === 1 ? 'folder was' : 'folders were'} left as they are:`] })
              : null,
            skipped.length
              ? jsx('ul', { className: 'sb-skipped', children: skipped.map(item => jsxs('li', { children: [jsx('code', { children: item.folder }), `: ${item.reason}`] }, item.folder)) })
              : null
          ]
        }),
        jsx('div', { className: 'sb-banner-actions', children: jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onDismiss, children: 'Done' }) })
      ]
    })
  }

  if (!adoptable) {
    return null
  }

  return jsxs('div', {
    className: 'sb-banner',
    'data-banner': 'adopt',
    children: [
      icon('inbox'),
      jsxs('div', {
        children: [
          jsx('b', { children: `${adoptable} older ${adoptable === 1 ? 'shader' : 'shaders'} can join the library` }),
          jsx('p', { children: 'Made before versions existed. Each is rendered again; folders that fail are listed and left alone.' })
        ]
      }),
      jsxs('div', {
        className: 'sb-banner-actions',
        children: [
          jsx('button', { type: 'button', className: 'sb-btn', onClick: onStart, children: `Add ${adoptable}` }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onDismiss, children: 'Not now' })
        ]
      })
    ]
  })
}

function ThumbsNotice({ missing, jobId, onStart, onDismiss, onDone }) {
  const job = useJob(jobId).data
  const doneRef = useRef(false)

  useEffect(() => {
    if (job && job.state === 'done' && !doneRef.current) {
      doneRef.current = true
      onDone()
    }
  }, [job && job.state])

  if (job && JOB_ACTIVE.has(job.state)) {
    return jsxs('div', {
      className: 'sb-banner',
      'data-banner': 'thumbs',
      children: [icon('file-media'), jsxs('div', { children: [jsx('b', { children: 'Making previews' }), jsx(JobLine, { job, verb: 'Made' })] })]
    })
  }

  if (job && job.state === 'done') {
    return null
  }

  if (!missing) {
    return null
  }

  return jsxs('div', {
    className: 'sb-banner',
    'data-banner': 'thumbs',
    children: [
      icon('file-media'),
      jsxs('div', {
        children: [
          jsx('b', { children: `${missing} ${missing === 1 ? 'shader has' : 'shaders have'} no preview image yet` }),
          jsx('p', { children: 'Hermes draws them one at a time, between your renders.' })
        ]
      }),
      jsxs('div', {
        className: 'sb-banner-actions',
        children: [
          jsx('button', { type: 'button', className: 'sb-btn', onClick: onStart, children: 'Make previews' }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onDismiss, children: 'Not now' })
        ]
      })
    ]
  })
}

function PickUp({ row, image, onOpen }) {
  return jsxs('section', {
    className: 'sb-pickup',
    'aria-label': 'Pick up where you left off',
    children: [
      jsx(Thumb, { row, image, live: false }),
      jsxs('div', {
        style: { minWidth: 0 },
        children: [
          jsx('small', { children: 'Pick up where you left off' }),
          jsxs('div', { className: 'sb-tname', style: { marginTop: 2 }, children: [jsx('b', { children: displayName(row.name) }), jsx('span', { className: 'sb-ver', children: `v${row.version}` })] }),
          jsx('div', { className: 'sb-tsub', children: `Edited ${ago(row.updatedAt)} by ${AUTHOR_WORDS[row.authorKind] || 'Hermes'}` })
        ]
      }),
      jsxs('div', {
        className: 'sb-pick-actions',
        children: [
          jsx('button', { type: 'button', className: 'sb-btn sb-primary', 'data-action': 'pickup-continue', onClick: () => continueInChat(row), children: [icon('comment-discussion'), 'Continue in chat'] }),
          jsx('button', { type: 'button', className: 'sb-btn', onClick: () => onOpen(row.id), children: 'Open' })
        ]
      })
    ]
  })
}

function EmptyState({ archived, filtered, onClear }) {
  if (archived) {
    return jsxs('div', { className: 'sb-empty', children: [icon('archive'), jsx('h2', { children: 'Nothing archived' }), jsx('p', { children: 'Archived shaders wait here.' })] })
  }

  if (filtered) {
    return jsxs('div', {
      className: 'sb-empty',
      children: [
        icon('search'),
        jsx('h2', { children: 'No shaders match' }),
        jsx('p', { children: 'Try another word, or clear the filters.' }),
        jsx('button', { type: 'button', className: 'sb-btn', onClick: onClear, children: 'Clear filters' })
      ]
    })
  }

  return jsxs('div', {
    className: 'sb-empty',
    'data-empty': 'library',
    children: [
      icon('symbol-color'),
      jsx('h2', { children: 'No shaders yet' }),
      jsx('p', { children: 'Ask Hermes for a shader in any chat. Every render lands here with a preview, its versions and its sliders.' }),
      jsx(FirstPrompt, {})
    ]
  })
}

/** The empty library's example prompt, one per mount, gone while the setting is off. */
function FirstPrompt() {
  const [prompt] = useState(pickPrompt)
  const settings = sdk.useQuery({ queryKey: qkey('settings'), queryFn: () => apiCall('/settings'), staleTime: 0 })
  const shown = settings.isError || Boolean(settings.data && settings.data.starter_prompt !== false)

  return jsxs('div', {
    style: { display: 'contents' },
    children: [
      shown ? jsx('div', { className: 'sb-prompt', children: prompt }) : null,
      jsxs('div', {
        className: 'sb-pick-actions',
        style: { justifyContent: 'center' },
        children: [
          jsx('button', { type: 'button', className: 'sb-btn sb-primary', 'data-action': 'empty-new', onClick: () => newShader(shown ? prompt : ''), children: [icon('add'), 'Start in a new chat'] }),
          shown
            ? jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'copy-prompt', onClick: () => copyText(prompt).then(ok => say(ok ? 'Prompt copied.' : 'Could not copy the prompt.', ok ? 'ok' : 'err')), children: 'Copy prompt' })
            : null
        ]
      })
    ]
  })
}

/** Close a popover on Escape or a click outside it; focus goes back to its button. */
function usePopover(open, setOpen, popRef, buttonRef) {
  useEffect(() => {
    if (!open) {
      return undefined
    }

    const onDown = event => {
      if (popRef.current && !popRef.current.contains(event.target) && !(buttonRef.current && buttonRef.current.contains(event.target))) {
        setOpen(false)
      }
    }

    const onKey = event => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        setOpen(false)
        buttonRef.current && buttonRef.current.focus()
      }
    }

    document.addEventListener('mousedown', onDown)
    popRef.current && popRef.current.addEventListener('keydown', onKey)
    const node = popRef.current
    node && node.querySelector('select,input,button') && node.querySelector('select,input,button').focus()

    return () => {
      document.removeEventListener('mousedown', onDown)
      node && node.removeEventListener('keydown', onKey)
    }
  }, [open])
}

function FilterPopover({ filters, facets, onChange, popRef }) {
  const field = (label, name, options) => jsxs('label', {
    children: [
      label,
      jsx('select', {
        className: 'sb-field',
        name,
        value: filters[name] || '',
        onChange: event => onChange({ [name]: event.target.value }),
        children: options.map(([value, text]) => jsx('option', { value, children: text }, value))
      })
    ]
  })

  return jsxs('div', {
    ref: popRef,
    className: 'sb-pop',
    role: 'group',
    'aria-label': 'Filters',
    children: [
      field('State', 'state', STATE_FILTERS),
      field('Licence', 'licence', [['', 'Any licence'], ['private', 'Private'], ...(facets.licences || []).map(id => [id, licenceLabel(id)])]),
      field('Tag', 'tag', [['', 'Any tag'], ...(facets.tags || []).map(tag => [tag, tag])]),
      jsxs('label', {
        className: 'sb-check',
        children: [
          jsx('input', { type: 'checkbox', name: 'starred', checked: filters.starred === '1', onChange: event => onChange({ starred: event.target.checked ? '1' : '' }) }),
          'Starred only'
        ]
      })
    ]
  })
}

const FILTER_KEYS = ['q', 'state', 'licence', 'tag', 'starred', 'sort']

function filterChipText(key, value) {
  if (key === 'state') {
    return (STATE_FILTERS.find(([id]) => id === value) || [value, value])[1]
  }

  if (key === 'licence') {
    return value === 'private' ? 'Private' : licenceLabel(value)
  }

  if (key === 'tag') {
    return `#${value}`
  }

  return key === 'starred' ? 'Starred' : value
}

function ShadersPage() {
  const query = useRouteQuery()
  const client = sdk.useQueryClient()
  const archived = query.get('archived') === '1'
  const openId = query.get('shader') || ''
  const labId = query.get('lab') || ''
  const view = query.get('view') || storeGet('view', 'grid')
  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [filters, setFilters] = useState({ state: '', licence: '', tag: '', starred: '', sort: 'updated' })
  const [filterOpen, setFilterOpen] = useState(false)
  const [moreLoading, setMoreLoading] = useState(false)
  const [bulkBusy, setBulkBusy] = useState(false)
  const bulkBusyRef = useRef(false)
  const [selected, setSelected] = useState(() => new Set())
  const [deleting, setDeleting] = useState(false)
  const [hoverId, setHoverId] = useState(null)
  const [activeIndex, setActiveIndex] = useState(0)
  const [adoptJob, setAdoptJob] = useState('')
  const [thumbJob, setThumbJob] = useState('')
  const [adoptHidden, setAdoptHidden] = useState(false)
  const [thumbsHidden, setThumbsHidden] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [reduced] = useState(prefersReducedMotion)
  const filterButton = useRef(null)
  const filterPop = useRef(null)
  const gridRef = useRef(null)
  const hoverTimer = useRef(0)
  const lastOpened = useRef('')
  const shellRef = useRef(null)
  const selectAllRef = useRef(null)
  // The last tile checkbox clicked, where a shift-click range starts.
  const anchorRef = useRef('')

  usePopover(filterOpen, setFilterOpen, filterPop, filterButton)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 250)

    return () => window.clearTimeout(timer)
  }, [search])

  const params = new URLSearchParams()

  if (debounced) {
    params.set('q', debounced)
  }

  for (const key of ['state', 'licence', 'tag', 'starred']) {
    if (filters[key]) {
      params.set(key, filters[key])
    }
  }

  if (filters.sort && filters.sort !== 'updated') {
    params.set('sort', filters.sort)
  }

  if (archived) {
    params.set('archived', '1')
  }

  const qs = params.toString()
  const library = useLibrary(qs)
  const page = library.data || null
  const rows = page ? page.rows : []
  // Placeholder data belongs to the previous filters, so its cursor is not this query's.
  const cursor = page && !library.isPlaceholderData ? page.cursor : null
  const filtered = Boolean(debounced || filters.state || filters.licence || filters.tag || filters.starred)
  const rowById = new Map(rows.map(row => [row.id, row]))
  const hovered = hoverId ? rowById.get(hoverId) : null
  const liveId = !openId && !reduced && hovered && hovered.autoplay ? hovered.id : null
  const runningAdopt = adoptJob || ((page && page.jobs) || []).find(job => job.kind === 'adopt' && JOB_ACTIVE.has(job.state))?.id || ''
  const runningThumbs = thumbJob || ((page && page.jobs) || []).find(job => job.kind === 'thumbs' && JOB_ACTIVE.has(job.state))?.id || ''
  const thumbs = useThumbs((page && page.pickUp ? [...rows, page.pickUp] : rows).map(rowThumbItem))
  const missingThumbs = rows.filter(row => !row.thumb && !row.thumbVersion).length

  const qsRef = useRef(qs)
  qsRef.current = qs
  const rowsRef = useRef(rows)
  rowsRef.current = rows

  useEffect(() => {
    setSelected(new Set())
    setDeleting(false)
    setActiveIndex(0)
  }, [qs])

  const clearSelection = () => {
    setSelected(new Set())
    setDeleting(false)
  }

  // Every shader of this tab and filter, also the ones not loaded yet.
  const selectAll = async () => {
    const asked = qs
    let ids = rows.map(row => row.id)

    try {
      const answer = await apiCall(`/library/ids${asked ? `?${asked}` : ''}`)
      ids = answer && Array.isArray(answer.ids) ? answer.ids : ids
    } catch (error) {
      say(`Selected the ${ids.length} shown; the full list did not load: ${problemText(error)}`, 'warn')
    }

    if (qsRef.current === asked) {
      setSelected(new Set(ids))
    }
  }

  const onHover = useCallback(id => {
    window.clearTimeout(hoverTimer.current)

    if (!id) {
      setHoverId(null)

      return
    }

    hoverTimer.current = window.setTimeout(() => setHoverId(id), HOVER_DELAY_MS)
  }, [])

  useEffect(() => () => window.clearTimeout(hoverTimer.current), [])

  const openShader = id => {
    lastOpened.current = id
    const index = rows.findIndex(row => row.id === id)

    if (index >= 0) {
      setActiveIndex(index)
    }

    patchPage(query, { shader: id, v: '', tab: '', compare: '' })
  }

  const closeSheet = () => {
    const id = openId || lastOpened.current
    patchPage(query, { shader: '', v: '', tab: '', compare: '' })
    window.setTimeout(() => {
      const node = gridRef.current && id ? gridRef.current.querySelector(`[data-tile-id="${window.CSS.escape(id)}"]`) : null
      node && node.focus()
    }, 30)
  }

  // Tiles are memoised, so they get callbacks that stay the same across renders.
  const openRef = useRef(openShader)
  openRef.current = openShader
  const onOpenTile = useCallback(id => openRef.current(id), [])

  const toggleSelect = useCallback((id, range) => {
    const list = rowsRef.current.map(row => row.id)
    const from = list.indexOf(anchorRef.current)
    const to = list.indexOf(id)
    anchorRef.current = id

    setSelected(current => {
      const next = new Set(current)

      if (range && from >= 0 && to >= 0) {
        list.slice(Math.min(from, to), Math.max(from, to) + 1).forEach(item => next.add(item))
      } else if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }

      return next
    })
  }, [])

  const loadMore = async () => {
    if (!cursor || moreLoading) {
      return
    }

    setMoreLoading(true)

    try {
      const next = await apiCall(libraryUrl(qs, cursor))
      const key = qkey('library', qs)
      const current = client.getQueryData(key)

      // A refetch that landed meanwhile already carries its own pages; this one is dropped.
      if (current && current.cursor === cursor) {
        client.setQueryData(key, mergePage(current, next))
      }
    } catch (error) {
      say(`Could not load more: ${problemText(error)}`, 'err')
    } finally {
      setMoreLoading(false)
    }
  }

  const setView = next => {
    storeSet('view', next)
    patchPage(query, { view: next })
  }

  const setTab = nextArchived => {
    setSearch('')
    setDebounced('')
    goPage({ view: query.get('view') || '', archived: nextArchived ? '1' : '' })
  }

  const clearFilters = () => {
    setSearch('')
    setDebounced('')
    setFilters({ state: '', licence: '', tag: '', starred: '', sort: filters.sort })
  }

  const startJob = async (kind, setId) => {
    try {
      const answer = await apiCall(`/jobs/${kind}`, { method: 'POST', body: {} })

      if (answer && answer.job) {
        client.setQueryData(qkey('job', answer.job.id), answer.job)
        setId(answer.job.id)
      }
    } catch (error) {
      say(`Could not start: ${problemText(error)}`, 'err')
    }
  }

  const bulkArchive = async () => {
    if (bulkBusyRef.current) {
      return
    }

    bulkBusyRef.current = true
    setBulkBusy(true)
    const ids = [...selected]
    let done = 0

    for (const id of ids) {
      try {
        await apiCall(`${shaderPath(id)}/${archived ? 'unarchive' : 'archive'}`, { method: 'POST', body: {} })
        done += 1
      } catch (error) {
        say(`Stopped at ${displayName(rowById.get(id)?.name)}: ${problemText(error)}`, 'err')
        break
      }
    }

    if (done) {
      say(`${archived ? 'Restored' : 'Archived'} ${done} ${done === 1 ? 'shader' : 'shaders'}.`, 'ok')
    }

    setSelected(new Set())
    bulkBusyRef.current = false
    setBulkBusy(false)
    invalidateShader(client)
    ids.forEach(id => client.invalidateQueries({ queryKey: qkey('detail', id) }))
  }

  const onGridKey = event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a' && !event.target.closest('input, textarea, select, [contenteditable=true]')) {
      event.preventDefault()
      selectAll()

      return
    }

    const nodes = gridRef.current ? [...gridRef.current.querySelectorAll('[data-tile-id]')] : []
    const index = nodes.indexOf(document.activeElement)

    if (index < 0) {
      return
    }

    const id = nodes[index].getAttribute('data-tile-id')

    if (event.key === 'Enter') {
      event.preventDefault()
      openShader(id)

      return
    }

    if (event.key === ' ') {
      event.preventDefault()
      toggleSelect(id)

      return
    }

    const cols = view === 'list' ? 1 : columnsOf(nodes)
    const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols, Home: -index, End: nodes.length - 1 - index }

    if (!(event.key in moves)) {
      return
    }

    const next = Math.min(nodes.length - 1, Math.max(0, index + moves[event.key]))
    event.preventDefault()

    if (next !== index) {
      setActiveIndex(next)
      nodes[next].focus()
    }
  }

  const escapeRef = useRef(null)
  escapeRef.current = () => {
    if (importOpen) {
      setImportOpen(false)
    } else if (openId) {
      closeSheet()
    } else {
      clearSelection()
    }
  }

  // Escape closes the sheet from anywhere in this page, also when focus fell back to the body
  // (a button that disabled itself); inner editors stop the event first.
  const selecting = selected.size > 0

  useEffect(() => {
    if (!openId && !importOpen && !selecting) {
      return undefined
    }

    const onKey = event => {
      const target = event.target
      const inPage = !target || target === document.body || target === document.documentElement || (shellRef.current && shellRef.current.contains(target))

      if (event.key === 'Escape' && !event.defaultPrevented && inPage) {
        event.preventDefault()
        escapeRef.current()
      }
    }

    document.addEventListener('keydown', onKey)

    return () => document.removeEventListener('keydown', onKey)
  }, [openId, importOpen, selecting])

  const total = page ? page.total : 0
  const allSelected = total > 0 && selected.size >= total

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selected.size > 0 && !allSelected
    }
  })
  const Item = view === 'list' ? ListRow : Tile

  const header = jsxs('header', {
    className: 'sb-phead',
    children: [
      jsx('h1', { children: 'Shaderbrücke' }),
      page ? jsx('span', { className: 'sb-count', children: `${total} ${total === 1 ? 'shader' : 'shaders'}${archived ? ' archived' : ''}` }) : null,
      page && rows.length && !labId
        ? jsx('input', { type: 'checkbox', ref: selectAllRef, className: 'sb-selall', 'data-action': 'select-all', checked: allSelected, 'aria-label': selected.size ? 'Clear the selection' : 'Select all shaders',
            title: selected.size ? 'Clear the selection (Escape)' : 'Select all shaders', onChange: () => (selected.size ? clearSelection() : selectAll()) })
        : null,
      jsx('span', { className: 'sb-grow' }),
      jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': 'import', onClick: () => setImportOpen(true), children: [icon('cloud-download'), 'Import'] }),
      jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'new-shader', onClick: () => newShader(), children: [icon('add'), 'New shader'] })
    ]
  })

  const tab = (selectedTab, props) => jsx('button', { type: 'button', role: 'tab', className: 'sb-tab', 'aria-selected': selectedTab ? 'true' : 'false', tabIndex: selectedTab ? 0 : -1, ...props })
  const tabs = jsxs('div', {
    className: 'sb-tabs',
    role: 'tablist',
    'aria-label': 'Library views',
    // Arrow keys, Home and End move between the tabs and open the one they land on.
    onKeyDown: event => {
      const list = [...event.currentTarget.querySelectorAll('[role=tab]')]
      const index = list.indexOf(document.activeElement)
      const moves = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: list.length - 1 - index }

      if (index < 0 || !(event.key in moves)) {
        return
      }

      event.preventDefault()
      const next = list[(index + moves[event.key] + list.length) % list.length]
      next.focus()
      next.click()
    },
    children: [
      tab(!archived && !labId, { onClick: () => setTab(false), children: 'My shaders' }),
      tab(archived && !labId, { 'data-tab': 'archived', onClick: () => setTab(true), children: 'Archived' }),
      labId ? tab(true, { 'data-tab': 'lab', onClick: () => goPage({ lab: labId }), children: [icon('beaker'), 'Lab'] }) : null
    ]
  })

  const activeFilters = ['state', 'licence', 'tag', 'starred'].filter(key => filters[key])

  const tools = jsxs('div', {
    className: 'sb-tools',
    children: [
      jsxs('label', {
        className: 'sb-search',
        children: [
          icon('search'),
          jsx('input', { type: 'search', value: search, placeholder: archived ? 'Search the archive' : 'Search my shaders', 'aria-label': 'Search shaders', onChange: event => setSearch(event.target.value) })
        ]
      }),
      jsx('button', {
        type: 'button',
        ref: filterButton,
        className: 'sb-btn',
        'aria-haspopup': 'true',
        'aria-expanded': filterOpen ? 'true' : 'false',
        'data-action': 'filter',
        onClick: () => setFilterOpen(open => !open),
        children: [icon('filter'), activeFilters.length ? `Filter (${activeFilters.length})` : 'Filter']
      }),
      jsx('select', {
        className: 'sb-field',
        'aria-label': 'Sort',
        name: 'sort',
        value: filters.sort,
        onChange: event => setFilters(current => ({ ...current, sort: event.target.value })),
        children: [['updated', 'Last edited'], ['created', 'Created'], ['name', 'Name']].map(([value, text]) => jsx('option', { value, children: text }, value))
      }),
      jsx('span', { className: 'sb-grow' }),
      jsxs('div', {
        className: 'sb-seg',
        role: 'group',
        'aria-label': 'View',
        children: [
          jsx('button', { type: 'button', 'aria-pressed': view !== 'list' ? 'true' : 'false', 'aria-label': 'Grid view', 'data-view': 'grid', onClick: () => setView('grid'), children: [icon('layout'), 'Grid'] }),
          jsx('button', { type: 'button', 'aria-pressed': view === 'list' ? 'true' : 'false', 'aria-label': 'List view', 'data-view': 'list', onClick: () => setView('list'), children: [icon('list-flat'), 'List'] })
        ]
      }),
      filterOpen
        ? jsx(FilterPopover, { filters, facets: (page && page.facets) || {}, popRef: filterPop, onChange: patch => setFilters(current => ({ ...current, ...patch })) })
        : null
    ]
  })

  const filterChips = activeFilters.length
    ? jsx('div', {
        className: 'sb-chips sb-active-filters',
        style: { margin: '-6px 0 14px' },
        children: activeFilters.map(key => jsxs('button', {
          type: 'button',
          className: 'sb-pchip sb-pchip-acc',
          'aria-label': `Remove filter ${filterChipText(key, filters[key])}`,
          onClick: () => setFilters(current => ({ ...current, [key]: '' })),
          children: [filterChipText(key, filters[key]), icon('close')]
        }, key))
      })
    : null

  let body = null
  // With placeholder or cached rows on screen, a failed fetch would otherwise pass unseen.
  const staleError = library.isError && page
    ? jsxs('p', {
        className: 'sb-inline-err',
        role: 'alert',
        'data-library-error': 'true',
        children: [`These results may be out of date: ${problemText(library.error)} `,
          jsx('button', { type: 'button', className: 'sb-link', onClick: () => library.refetch(), children: 'Try again' })]
      })
    : null

  if (library.isError && !page) {
    body = jsxs('div', {
      className: 'sb-empty',
      role: 'alert',
      children: [icon('error', 'sb-err'), jsx('h2', { children: 'The library did not load' }), jsx('p', { children: problemText(library.error) }),
        jsx('button', { type: 'button', className: 'sb-btn', onClick: () => library.refetch(), children: 'Try again' })]
    })
  } else if (!page) {
    body = jsx('div', { className: 'sb-grid', 'aria-busy': 'true', children: Array.from({ length: 6 }, (_, i) => jsxs('div', { className: 'sb-skel', children: [jsx('div', { className: 'sb-thumb' }), jsx('div', { className: 'sb-skel-line' }), jsx('div', { className: 'sb-skel-line sb-skel-short' })] }, i)) })
  } else if (!rows.length) {
    body = jsx(EmptyState, { archived, filtered, onClear: clearFilters })
  } else {
    const active = Math.min(activeIndex, rows.length - 1)
    body = jsx('div', {
      ref: gridRef,
      className: `${view === 'list' ? 'sb-list' : 'sb-grid'}${selected.size ? ' sb-selecting' : ''}`,
      role: 'list',
      'aria-label': archived ? 'Archived shaders' : 'My shaders',
      onKeyDown: onGridKey,
      children: rows.map((row, index) => jsx(Item, {
        row,
        image: thumbOf(row, thumbs),
        active: index === active,
        open: row.id === openId,
        selected: selected.has(row.id),
        live: row.id === liveId,
        onOpen: onOpenTile,
        onSelect: toggleSelect,
        onHover
      }, row.id))
    })
  }

  const bulk = selected.size
    ? jsxs('div', {
        className: 'sb-bulk',
        children: [deleting ? jsx(BulkDeleteForm, { client, ids: [...selected], rowById, onCancel: () => setDeleting(false), onDone: clearSelection }, 'delete') : null, jsxs('div', {
          className: 'sb-bulkbar',
          role: 'toolbar',
          'aria-label': 'Selected shaders',
          children: [
            jsx('span', { className: 'sb-count', 'data-bulk-count': selected.size, children: allSelected && total > 1 ? `All ${selected.size} selected` : `${selected.size} selected` }),
            jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'bulk-archive', disabled: bulkBusy, onClick: bulkArchive, children: [icon(archived ? 'discard' : 'archive'), archived ? 'Restore' : 'Archive'] }),
            jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'bulk-share', onClick: () => saveShareFiles([...selected].map(id => ({ id }))), children: [icon('save'), 'Share files'] }),
            jsx('button', { type: 'button', className: 'sb-btn sb-danger-btn', 'data-action': 'bulk-delete', 'aria-expanded': deleting ? 'true' : 'false', disabled: bulkBusy, onClick: () => setDeleting(open => !open), children: [icon('trash'), 'Delete'] })
          ]
        }, 'bar')]
      })
    : null

  const showPickUp = page && page.pickUp && !archived && !filtered && rows.length > 0

  if (labId) {
    // A Desktop without host.openWorkspace: the Lab is a tab of this page.
    return jsx('div', {
      className: 'sb-shell',
      'data-shaderbruecke-page': 'true',
      ref: shellRef,
      children: jsx('div', {
        className: 'sb-page',
        children: jsxs('div', {
          className: 'sb-wrap sb-lab-tab',
          children: [header, tabs, jsx('div', { className: 'sb-lab-tabbody', children: jsx(LabRoot, { id: labId, onBack: () => goPage({ shader: labId }) }, labId) })]
        })
      })
    })
  }

  return jsxs('div', {
    className: 'sb-shell',
    'data-shaderbruecke-page': 'true',
    ref: shellRef,
    children: [
      jsx('div', {
        className: 'sb-page',
        children: jsxs('div', {
          className: 'sb-wrap',
          children: [
            header,
            tabs,
            tools,
            filterChips,
            staleError,
            !archived && !adoptHidden && page
              ? jsx(AdoptBanner, {
                  adoptable: page.adoptable || 0,
                  jobId: runningAdopt,
                  onStart: () => startJob('adopt', setAdoptJob),
                  onDismiss: () => {
                    setAdoptHidden(true)
                    setAdoptJob('')
                  },
                  onDone: () => invalidateShader(client)
                })
              : null,
            !archived && !thumbsHidden && page
              ? jsx(ThumbsNotice, { missing: missingThumbs, jobId: runningThumbs, onStart: () => startJob('thumbs', setThumbJob), onDismiss: () => setThumbsHidden(true), onDone: () => invalidateShader(client) })
              : null,
            showPickUp ? jsx(PickUp, { row: page.pickUp, image: thumbOf(page.pickUp, thumbs), onOpen: openShader }) : null,
            body,
            cursor
              ? jsx('div', { className: 'sb-more', children: jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'load-more', disabled: moreLoading, onClick: loadMore,
                  children: moreLoading ? [icon('loading', 'codicon-modifier-spin'), 'Loading'] : `Load more (${Math.max(0, total - rows.length)} left)` }) })
              : null,
            bulk
          ]
        })
      }),
      importOpen
        ? [
            jsx('div', { className: 'sb-dim', 'aria-hidden': 'true', onClick: () => setImportOpen(false) }, 'idim'),
            jsx(ImportPanel, { key: 'import', onClose: () => setImportOpen(false), onOpenShader: id => {
              setImportOpen(false)
              openShader(id)
            } })
          ]
        : openId
        ? [
            jsx('div', { className: 'sb-dim', 'aria-hidden': 'true', onClick: closeSheet }, 'dim'),
            jsx(ShaderSheet, { key: openId, id: openId, query, archived, onClose: closeSheet, onOpenShader: openShader })
          ]
        : null,
      jsx(Toaster, { scope: 'page' })
    ]
  })
}

/** The time and play controls under a stage, shared by the sheet and the Lab. */
function PlayerBar({ controlRef, phase, scrub, onScrub, label }) {
  const playing = phase === 'live'
  const scrubMax = Math.max(10, Math.ceil((scrub + 5) / 5) * 5)

  return jsxs('div', {
    className: 'sb-player',
    children: [
      jsx('button', {
        type: 'button',
        className: 'sb-icon-btn',
        'aria-pressed': playing ? 'true' : 'false',
        'aria-label': playing ? `Pause ${label}` : `Play ${label}`,
        'data-action': 'play',
        disabled: phase === 'lint' || phase === 'error' || phase === 'lost' || phase === 'loading',
        onClick: () => controlRef.current && controlRef.current.toggle(),
        children: icon(playing ? 'debug-pause' : 'play')
      }),
      jsx('span', { className: 'sb-ptime', children: formatTime(scrub) }),
      jsx('input', {
        type: 'range',
        min: 0,
        max: scrubMax,
        step: 0.01,
        value: scrub,
        'aria-label': 'Time in seconds',
        'aria-valuetext': `${scrub.toFixed(2)} seconds`,
        onChange: event => {
          const t = Number(event.target.value)

          if (controlRef.current && playing) {
            controlRef.current.pause()
          }

          controlRef.current && controlRef.current.seek(t)
          onScrub(t)
        }
      })
    ]
  })
}

/**
 * A stage with the autoplay rule: a version that may autoplay plays; any other shows its still and
 * a gate with Play; a flash `block` needs a second confirmation before it plays. The Lab passes
 * its own `gate` and owns the decision (`consent`, `onConsent`), so it survives recompiles.
 */
function Stage({ doc, label, values, controlRef, onPhaseChange, gate: gateProp, consent, onConsent }) {
  const gate = gateProp !== undefined ? gateProp : playGate(doc)
  const controlled = consent !== undefined
  const [armedHere, setArmed] = useState(!gate)
  const armed = controlled ? consent || !gate : armedHere
  const [confirming, setConfirming] = useState(false)
  const [phase, setPhase] = useState('loading')
  const [scrub, setScrub] = useState(1)
  const [reload, setReload] = useState(0)
  const lastTick = useRef(0)

  useEffect(() => {
    setArmed(!gate)
    setConfirming(false)
  }, [doc.id, doc.version])

  const onPhase = state => {
    setPhase(state.phase)
    onPhaseChange && onPhaseChange(state)

    if (state.phase !== 'live' && controlRef.current) {
      setScrub(controlRef.current.time())
    }
  }

  const onTime = seconds => {
    const now = performance.now()

    if (now - lastTick.current > 120) {
      lastTick.current = now
      setScrub(seconds)
    }
  }

  const play = () => {
    if (gate && gate.level === 'block' && !confirming) {
      setConfirming(true)

      return
    }

    if (controlled) {
      onConsent()
    } else {
      setArmed(true)
    }

    setConfirming(false)
    controlRef.current && controlRef.current.play()
  }

  const problem = phase === 'lint' || phase === 'error'

  return jsxs('div', {
    className: 'sb-stagebox',
    'data-phase': phase,
    children: [
      jsxs('div', {
        className: 'sb-pstage',
        children: [
          phase === 'nowebgl' && doc.thumb ? jsx('img', { src: doc.thumb, alt: `${label}, still preview` }) : null,
          jsx(LivePlayer, {
            source: doc.source,
            sliders: doc.sliders || [],
            values,
            autoplay: controlled ? armed : !gate,
            reload,
            className: 'sb-pcanvas',
            label: `${label}, shader preview`,
            controlRef,
            onPhase,
            onTime
          })
        ]
      }),
      problem
        ? jsxs('div', { className: 'sb-problem sb-problem-flat', role: 'alert', children: [jsx('h4', { children: [icon('error', 'sb-err'), phase === 'lint' ? 'Not compiled: the local lint stopped it' : 'This GPU could not compile it'] })] })
        : phase === 'lost'
          ? jsxs('div', {
              className: 'sb-problem sb-problem-flat',
              role: 'alert',
              'data-lost': 'true',
              children: [
                jsx('h4', { children: [icon('warning', 'sb-warn'), 'The GPU dropped this preview'] }),
                jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'reload-preview', onClick: () => setReload(n => n + 1), children: [icon('refresh'), 'Reload preview'] })
              ]
            })
          : null,
      gate && !armed
        ? jsxs('div', {
            className: 'sb-gate',
            'data-gate': gate.level,
            role: 'note',
            children: [
              jsx(Chip, { tone: gate.tone, glyph: gate.glyph, children: gate.word }),
              jsx('p', { children: confirming ? 'It can flash quickly. Play it only if flashing light is safe for you.' : gate.text }),
              jsx('button', { type: 'button', className: 'sb-btn', 'data-action': confirming ? 'play-confirm' : 'play-gated', onClick: play,
                children: confirming ? 'Play anyway' : [icon('play'), 'Play'] })
            ]
          })
        : jsx(PlayerBar, { controlRef, phase, scrub, onScrub: setScrub, label })
    ]
  })
}

function useShaderDetail(id) {
  return sdk.useQuery({ queryKey: qkey('detail', id), queryFn: () => apiCall(shaderPath(id)), enabled: Boolean(id) })
}

function SheetExport({ id, version, name }) {
  const client = sdk.useQueryClient()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState('')
  const refocusRef = useRef(true)
  const menuId = `sb-sheet-export-menu-${useId()}`
  const versionQuery = version ? `?version=${encodeURIComponent(version)}` : ''
  const targetsQuery = sdk.useQuery({ queryKey: qkey('targets', id, version || 0), queryFn: () => apiCall(`${shaderPath(id)}/targets${versionQuery}`), enabled: open })
  const targets = targetsQuery.data && Array.isArray(targetsQuery.data.targets) ? targetsQuery.data.targets : []
  const title = displayName(name)

  const close = refocus => {
    refocusRef.current = refocus
    setOpen(false)
  }

  const askToFixExport = (row, problem) => {
    close(false)

    return draftInNewChat(exportFixRequest(title, id, version, row, problem), `Export: ${title}`)
      .then(result => say(result.ok ? 'The export request is drafted in a new chat.' : 'Could not open a chat, so the request is on the clipboard.', result.ok ? 'ok' : 'warn'))
  }

  const exportNow = async row => {
    const label = targetName(row, true)
    setBusy(row.id)
    say(`Exporting ${label} and proving it. This can take a minute.`, 'none')

    try {
      const answer = await apiCall(`${shaderPath(id)}/export/${encodeURIComponent(row.id)}${versionQuery}`, { method: 'POST', body: {}, timeoutMs: LONG_MS })
      client.invalidateQueries({ queryKey: qkey('targets', id) })
      const verdict = answer && answer.proof ? answer.proof.verdict : 'not_proven'

      if (answer && answer.success === false) {
        say(`${label} export failed: ${answer.error || 'no reason given'}.`, 'err', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `failed: ${answer.error || 'no reason given'}`) }])
      } else {
        say(verdict === 'match' ? `${label} exported and proven: match.` : `${label} exported, ${verdict === 'differs' ? 'but the proof differs' : 'not proven'}.`, verdict === 'match' ? 'ok' : 'warn')
      }
    } catch (error) {
      say(`${label} export failed: ${problemText(error)}`, 'err', [{ label: 'Ask Hermes to fix it', run: () => askToFixExport(row, `failed: ${problemText(error)}`) }])
    } finally {
      setBusy('')
    }
  }

  const fileText = async row => {
    try {
      return await apiCall(`${shaderPath(id)}/export/${encodeURIComponent(row.id)}${versionQuery}`)
    } catch (error) {
      const problem = restProblem(error)

      if (problem.status === 409) {
        say(`${targetName(row, true)} isn't exported yet.`, 'warn', [{ label: 'Export now', run: () => exportNow(row) }])
      } else {
        say(`Could not read ${targetName(row, true)}: ${problemText(error)}`, 'err')
      }

      return null
    }
  }

  const copyFile = async row => {
    close(true)
    const answer = await fileText(row)

    if (answer && typeof answer.text === 'string') {
      const ok = await copyText(answer.text)
      say(ok ? `${answer.main_file || targetName(row, true)} copied.` : 'Could not copy the file.', ok ? 'ok' : 'err')
    }
  }

  const sendToChat = async row => {
    close(false)
    const answer = await fileText(row)

    if (answer && typeof answer.text === 'string') {
      const result = await draftInNewChat(answer.text, `${targetName(row, true)}: ${title}`)
      say(result.ok ? `${answer.main_file || targetName(row, true)} is drafted in a new chat.` : 'Could not open a chat, so the file is on the clipboard.', result.ok ? 'ok' : 'warn')
    }
  }

  return jsx(ExportPopover, {
    id: menuId,
    open,
    onOpenChange: setOpen,
    refocusRef,
    trigger: jsx('button', {
      type: 'button',
      className: 'sb-btn',
      'aria-controls': menuId,
      'data-action': 'export',
      children: [icon('export'), 'Export', icon('chevron-down')]
    }),
    children: targets.length
      ? jsx(ExportMenu, { targets, busy, onCopy: copyFile, onSend: sendToChat, onExport: exportNow })
      : jsx('div', { className: 'sb-mstatus', role: 'status', children: targetsQuery.isError ? `Targets did not load: ${problemText(targetsQuery.error)}` : 'Loading targets' })
  })
}

const TAG_MAX = 8
const TAG_CHARS = 24

/** Tags are lowercase words: spaces become hyphens, other characters drop, at most 24 characters. */
function normaliseTag(text) {
  return String(text || '').trim().toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, TAG_CHARS)
}

async function patchShader(client, id, body) {
  const answer = await apiCall(shaderPath(id), { method: 'PATCH', body })

  if (answer && answer.detail && answer.detail.row && answer.detail.row.id === id) {
    client.setQueryData(qkey('detail', id), answer.detail)
    invalidateShader(client)
  } else {
    invalidateShader(client, id)
  }

  return answer
}

const SHEET_SLIDER_WORDS = {
  saved: ['Saved, used in exports and the next preview.', 'ok'],
  reset: ['Back to the authored defaults.', 'ok'],
  'save-failed': ['Not saved: ', 'err'],
  'reset-failed': ['Reset not saved: ', 'err']
}

function SheetSliders({ id, doc, controlRef, live }) {
  const sliders = doc.sliders || []
  const { values, set, resetAll, flush } = useSliderCommit({
    id: live ? id : '',
    sliders,
    initial: doc.effectiveValues || doc.tunedValues,
    controlRef,
    notify: (kind, error) => {
      const [text, tone] = SHEET_SLIDER_WORDS[kind]
      say(error ? `${text}${problemText(error)}` : text, tone)
    }
  })

  if (!sliders.length) {
    return null
  }

  const tuned = sliders.some(slider => Math.abs((values[slider.name] ?? slider.default) - slider.default) > 1e-9)

  return jsxs('section', {
    className: 'sb-sec',
    'aria-label': 'Sliders',
    children: [
      jsxs('h3', {
        children: [
          'Sliders',
          live ? null : jsx('span', { className: 'sb-count', style: { fontWeight: 400 }, children: 'Moves this preview only; tuning is saved on the latest version.' }),
          jsx('span', { className: 'sb-grow' }),
          tuned ? jsx('button', { type: 'button', className: 'sb-link', onClick: resetAll, children: 'Reset all' }) : null
        ]
      }),
      ...sliders.map(slider => jsx(SliderRow, {
        slider,
        value: values[slider.name] ?? slider.default,
        onChange: (name, value) => set(name, value, false),
        onBlur: flush,
        onReset: () => set(slider.name, slider.default, true)
      }, slider.name))
    ]
  })
}

function LicencePicker({ client, detail }) {
  const row = detail.row
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState('')
  const options = detail.licenceOptions || []
  const current = options.find(option => option.id === (row.licence || null))
  const name = `sb-licence-${row.id}`

  const choose = async option => {
    if (!option.allowed || option.id === (row.licence || null)) {
      return
    }

    setSaving(option.id || 'private')
    setError('')

    try {
      await patchShader(client, row.id, { licence: option.id })
      say(`Licence set to ${option.label}. The latest version carries it; earlier versions keep theirs.`, 'ok')
      setOpen(false)
    } catch (failure) {
      const problem = restProblem(failure)
      const allowed = (problem.detail.allowed || []).map(licenceLabel).join(', ')
      setError(problem.code === 'licence_refused' ? `${problem.message}${allowed ? ` Allowed: ${allowed}.` : ''}` : problemText(failure))
    } finally {
      setSaving('')
    }
  }

  return jsxs('dd', {
    className: 'sb-licence',
    children: [
      licenceChip(row),
      current ? jsx('span', { className: 'sb-count', children: current.meaning }) : null,
      jsx('button', { type: 'button', className: 'sb-link', 'aria-expanded': open ? 'true' : 'false', 'data-action': 'licence', onClick: () => setOpen(value => !value), children: open ? 'Close' : 'Change' }),
      open
        ? jsxs('div', {
            className: 'sb-lic-list',
            role: 'radiogroup',
            'aria-label': 'Licence',
            children: [
              row.licenceInherited
                ? jsx('p', { className: 'sb-note', 'data-inherited': 'true', children: `The imported source is ${licenceLabel(row.licenceInherited)}, so this shader keeps that licence or a later version of the same licence. It travels with every version and export.` })
                : jsx('p', { className: 'sb-note', children: 'Nothing is published in this version; the licence goes into export headers and share files.' }),
              ...options.map(option => {
                const checked = option.id === (row.licence || null)

                return jsxs('label', {
                  className: 'sb-lic-opt',
                  'aria-disabled': option.allowed ? undefined : 'true',
                  'data-licence': option.id || 'private',
                  children: [
                    jsx('input', { type: 'radio', name, checked, disabled: !option.allowed || Boolean(saving), onChange: () => choose(option) }),
                    jsxs('span', {
                      children: [
                        jsx('b', { children: option.label }),
                        saving === (option.id || 'private') ? jsx('span', { className: 'sb-count', children: ' Saving' }) : null,
                        jsx('small', { children: option.meaning }),
                        option.allowed ? null : jsx('small', { className: 'sb-reason', children: option.reason || 'Not available for this shader.' })
                      ]
                    })
                  ]
                }, option.id || 'private')
              }),
              error ? jsx('p', { className: 'sb-inline-err', role: 'alert', children: error }) : null
            ]
          })
        : null
    ]
  })
}

function TagEditor({ client, row }) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const tags = row.tags || []

  const save = async next => {
    setError('')

    try {
      await patchShader(client, row.id, { tags: next })
    } catch (failure) {
      setError(problemText(failure))
    }
  }

  const add = () => {
    const tag = normaliseTag(text)

    if (!tag) {
      return
    }

    if (tags.includes(tag)) {
      setText('')

      return
    }

    if (tags.length >= TAG_MAX) {
      setError(`At most ${TAG_MAX} tags.`)

      return
    }

    setText('')
    save([...tags, tag])
  }

  return jsxs('dd', {
    className: 'sb-tags',
    children: [
      ...tags.map(tag => jsxs('span', {
        className: 'sb-pchip',
        'data-tag': tag,
        children: [`#${tag}`, jsx('button', { type: 'button', className: 'sb-chip-x', 'aria-label': `Remove tag ${tag}`, onClick: () => save(tags.filter(t => t !== tag)), children: icon('close') })]
      }, tag)),
      tags.length < TAG_MAX
        ? jsx('input', {
            className: 'sb-field sb-tag-input',
            value: text,
            maxLength: TAG_CHARS + 8,
            placeholder: 'Add tag',
            'aria-label': 'Add a tag (lowercase, up to 24 characters)',
            onChange: event => setText(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter' || event.key === ',') {
                event.preventDefault()
                add()
              } else if (event.key === 'Escape' && text) {
                event.stopPropagation()
                setText('')
              }
            },
            onBlur: () => text && add()
          })
        : null,
      error ? jsx('span', { className: 'sb-inline-err', role: 'alert', children: error }) : null
    ]
  })
}

function RenameForm({ client, row, onDone }) {
  const [text, setText] = useState(row.name)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current && inputRef.current.select()
  }, [])

  const save = async event => {
    event.preventDefault()
    const name = text.trim()

    if (!name || name === row.name) {
      onDone()

      return
    }

    try {
      await patchShader(client, row.id, { name })
      say(`Renamed to ${displayName(name)}. Exports and share files use the new name; versions keep the one they were made with.`, 'ok')
      onDone()
    } catch (failure) {
      setError(problemText(failure))
    }
  }

  return jsxs('form', {
    className: 'sb-inline-form',
    'data-form': 'rename',
    onSubmit: save,
    children: [
      jsx('label', { className: 'sb-sr', htmlFor: `sb-rename-${row.id}`, children: 'New name' }),
      jsx('input', {
        id: `sb-rename-${row.id}`,
        ref: inputRef,
        className: 'sb-field',
        value: text,
        maxLength: 120,
        onChange: event => setText(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Escape') {
            event.stopPropagation()
            onDone()
          }
        }
      }),
      jsx('button', { type: 'submit', className: 'sb-btn', children: 'Save name' }),
      jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onDone, children: 'Cancel' }),
      error ? jsx('p', { className: 'sb-inline-err', role: 'alert', children: error }) : null
    ]
  })
}

function DeleteForm({ client, row, onDeleted, onCancel }) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = text.trim() === row.name

  const submit = async event => {
    event.preventDefault()

    if (!matches) {
      return
    }

    setBusy(true)

    try {
      await apiCall(shaderPath(row.id), { method: 'DELETE', body: { confirm: text.trim() } })
      say(`${displayName(row.name)} is deleted.`, 'ok')
      storeRemove(labKey(row.id))
      client.removeQueries({ queryKey: qkey('detail', row.id) })
      invalidateShader(client)
      onDeleted()
    } catch (failure) {
      setError(problemText(failure))
      setBusy(false)
    }
  }

  return jsxs('form', {
    className: 'sb-danger',
    'data-form': 'delete',
    onSubmit: submit,
    children: [
      jsx('b', { children: 'Delete forever' }),
      jsx('p', { children: `Every version, frame and preview of ${displayName(row.name)} is removed from the library; copies already exported into a project folder stay. Type its name to confirm.` }),
      jsx('input', {
        className: 'sb-field',
        value: text,
        autoFocus: true,
        'aria-label': `Type ${row.name} to confirm`,
        placeholder: row.name,
        onChange: event => setText(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Escape') {
            event.stopPropagation()
            onCancel()
          }
        }
      }),
      jsxs('div', {
        className: 'sb-pick-actions',
        children: [
          jsx('button', { type: 'submit', className: 'sb-btn sb-danger-btn', disabled: !matches || busy, 'data-action': 'delete-confirm', children: [icon('trash'), 'Delete forever'] }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onCancel, children: 'Cancel' })
        ]
      }),
      error ? jsx('p', { className: 'sb-inline-err', role: 'alert', children: error }) : null
    ]
  })
}

const BULK_NAMES = 3

/** The selection bar's Delete: one POST /library/delete after `delete` is typed. */
function BulkDeleteForm({ client, ids, rowById, onDone, onCancel }) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = text.trim() === 'delete'
  const names = ids.map(id => rowById.get(id)).filter(Boolean).slice(0, BULK_NAMES).map(row => displayName(row.name))
  const more = ids.length - names.length
  const count = `${ids.length} ${ids.length === 1 ? 'shader' : 'shaders'}`

  const submit = async event => {
    event.preventDefault()

    if (!matches || busy) {
      return
    }

    setBusy(true)

    try {
      const answer = await apiCall('/library/delete', { method: 'POST', body: { ids, confirm: 'delete' } })
      const deleted = (answer && answer.deleted) || []
      const missing = (answer && answer.missing) || []

      for (const id of deleted) {
        storeRemove(labKey(id))
        client.removeQueries({ queryKey: qkey('detail', id) })
      }

      say(`Deleted ${deleted.length} ${deleted.length === 1 ? 'shader' : 'shaders'}.${missing.length ? ` ${missing.length} ${missing.length === 1 ? 'was' : 'were'} already gone.` : ''}`, 'ok')
      invalidateShader(client)
      onDone()
    } catch (failure) {
      setError(problemText(failure))
      setBusy(false)
    }
  }

  return jsxs('form', {
    className: 'sb-danger sb-bulkdel',
    'data-form': 'bulk-delete',
    onSubmit: submit,
    children: [
      jsx('b', { children: `Delete ${count} forever` }),
      jsx('p', { children: `${names.join(', ')}${more > 0 ? `${names.length ? ' and ' : ''}${more} more` : ''}. Every version, frame and preview is removed from the library; copies already exported into a project folder stay. Type delete to confirm.` }),
      jsx('input', {
        className: 'sb-field',
        value: text,
        autoFocus: true,
        'aria-label': 'Type delete to confirm',
        placeholder: 'delete',
        onChange: event => setText(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Escape') {
            event.stopPropagation()
            event.nativeEvent.stopImmediatePropagation()
            onCancel()
          }
        }
      }),
      jsxs('div', {
        className: 'sb-pick-actions',
        children: [
          jsx('button', { type: 'submit', className: 'sb-btn sb-danger-btn', disabled: !matches || busy, 'data-action': 'bulk-delete-confirm', children: [icon('trash'), `Delete ${count}`] }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', onClick: onCancel, children: 'Cancel' })
        ]
      }),
      error ? jsx('p', { className: 'sb-inline-err', role: 'alert', children: error }) : null
    ]
  })
}

function FrameThumb({ thumb, label }) {
  return jsx('span', {
    className: 'sb-fthumb',
    children: thumb ? jsx('img', { src: thumb, alt: '', draggable: false }) : jsx('span', { className: 'sb-fnone', 'aria-label': `${label}, no preview` })
  })
}

function Filmstrip({ detail, current, ticks, onOpen, onTick }) {
  const versions = [...(detail.versions || [])].reverse()
  const id = detail.row.id
  // Version summaries carry no image: thumbStamp says one exists, the batch brings it.
  const thumbs = useThumbs(versions.map(version => (version.thumb || version.thumbStamp === null || version.thumbStamp === undefined ? null : thumbItem(id, version.version, version.thumbStamp))))

  return jsxs('section', {
    className: 'sb-sec',
    'aria-label': 'Versions',
    children: [
      jsxs('h3', { children: [`Versions (${versions.length})`, jsx('span', { className: 'sb-count', style: { fontWeight: 400, marginLeft: 8 }, children: versions.length > 1 ? 'Tick two to compare' : '' })] }),
      jsx('ol', {
        className: 'sb-film',
        children: versions.map(version => {
          const gate = playGate(version)
          const thumb = version.thumb || thumbs[`${id}@v${version.version}`]
          const ticked = ticks.includes(version.version)

          return jsxs('li', {
            className: 'sb-frame',
            'data-version': version.version,
            'aria-current': version.version === current ? 'true' : undefined,
            children: [
              jsxs('button', {
                type: 'button',
                className: 'sb-frame-open',
                'aria-label': `Open v${version.version}, by ${AUTHOR_WORDS[version.authorKind] || 'Hermes'}, ${ago(version.createdAt)}`,
                onClick: () => onOpen(version.version),
                children: [
                  jsx(FrameThumb, { thumb, label: `v${version.version}` }),
                  jsxs('span', { className: 'sb-frame-line', children: [jsx('b', { children: `v${version.version}` }), jsx('span', { children: AUTHOR_WORDS[version.authorKind] || 'Hermes' })] }),
                  jsx('span', { className: 'sb-frame-date', children: ago(version.createdAt) })
                ]
              }),
              gate ? jsx(Chip, { tone: gate.tone, glyph: gate.glyph, children: gate.word }) : null,
              detail.versions.length > 1
                ? jsxs('label', {
                    className: 'sb-frame-tick',
                    children: [
                      jsx('input', { type: 'checkbox', checked: ticked, disabled: !ticked && ticks.length >= 2, 'aria-label': `Compare v${version.version}`, onChange: () => onTick(version.version) }),
                      'Compare'
                    ]
                  })
                : null
            ]
          }, version.version)
        })
      })
    ]
  })
}

const DIFF_CONTEXT = 3

/** Unified diff rows with long unchanged runs folded to a count. */
function foldDiff(lines) {
  const keep = lines.map(() => false)

  lines.forEach((line, index) => {
    if (line.op !== ' ') {
      for (let i = Math.max(0, index - DIFF_CONTEXT); i <= Math.min(lines.length - 1, index + DIFF_CONTEXT); i++) {
        keep[i] = true
      }
    }
  })

  const out = []
  let skipped = 0

  lines.forEach((line, index) => {
    if (keep[index]) {
      if (skipped) {
        out.push({ fold: skipped })
        skipped = 0
      }

      out.push(line)
    } else {
      skipped += 1
    }
  })

  if (skipped) {
    out.push({ fold: skipped })
  }

  return out
}

function CompareView({ id, a, b, onClose }) {
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  const compare = sdk.useQuery({ queryKey: qkey('compare', id, lo, hi), queryFn: () => apiCall(`${shaderPath(id)}/compare?a=${lo}&b=${hi}`) })
  const data = compare.data

  return jsxs('section', {
    className: 'sb-compare',
    'aria-label': `Compare v${lo} and v${hi}`,
    'data-compare': `${lo},${hi}`,
    children: [
      jsxs('div', {
        className: 'sb-cmp-head',
        children: [
          jsx('b', { children: `v${lo} and v${hi}` }),
          data ? jsx('span', { className: 'sb-count', children: `${data.lines.filter(l => l.op === '+').length} lines added, ${data.lines.filter(l => l.op === '-').length} removed` }) : null,
          jsx('span', { className: 'sb-grow' }),
          jsx('button', { type: 'button', className: 'sb-icon-btn', 'aria-label': 'Close the comparison', onClick: onClose, children: icon('close') })
        ]
      }),
      compare.isError ? jsx('p', { className: 'sb-inline-err', role: 'alert', children: problemText(compare.error) }) : null,
      data
        ? jsxs('div', {
            children: [
              jsx('div', {
                className: 'sb-cmp-grid',
                children: [data.a, data.b].map(side => jsxs('figure', {
                  className: 'sb-cmp-cell',
                  children: [
                    jsx('div', { className: 'sb-thumb', children: side.thumb ? jsx('img', { src: side.thumb, alt: `v${side.version} at 1 s` }) : jsx('div', { className: 'sb-nothumb', children: jsx('span', { children: 'No preview yet' }) }) }),
                    jsx('figcaption', { children: `v${side.version}, ${AUTHOR_WORDS[side.authorKind] || 'Hermes'}, ${ago(side.createdAt)}` })
                  ]
                }, side.version))
              }),
              data.sliders.some(s => s.change !== 'same')
                ? jsx('ul', {
                    className: 'sb-cmp-sliders',
                    'aria-label': 'Slider changes',
                    children: data.sliders.filter(s => s.change !== 'same').map(s => jsxs('li', {
                      'data-change': s.change,
                      children: [
                        jsx(Chip, { tone: s.change === 'added' ? 'ok' : s.change === 'removed' ? 'err' : 'warn', children: s.change }),
                        jsx('code', { children: s.name }),
                        jsx('span', { className: 'sb-count', children: s.change === 'changed'
                          ? `${s.a.min}..${s.a.max} = ${s.a.default} becomes ${s.b.min}..${s.b.max} = ${s.b.default}`
                          : s.change === 'added' ? `${s.b.min}..${s.b.max} = ${s.b.default}` : `was ${s.a.min}..${s.a.max} = ${s.a.default}` })
                      ]
                    }, s.name))
                  })
                : jsx('p', { className: 'sb-count', children: 'Same sliders in both versions.' }),
              jsx('div', {
                className: 'sb-diff',
                role: 'region',
                'aria-label': 'Source changes',
                tabIndex: 0,
                children: data.lines.every(l => l.op === ' ')
                  ? jsx('p', { className: 'sb-count', style: { padding: '8px 10px', margin: 0 }, children: 'The source is the same; only the record differs.' })
                  : foldDiff(data.lines).map((line, index) => (line.fold
                      ? jsx('div', { className: 'sb-dfold', children: `${line.fold} unchanged ${line.fold === 1 ? 'line' : 'lines'}` }, `f${index}`)
                      : jsxs('div', {
                          className: 'sb-dline',
                          'data-op': line.op === '+' ? 'add' : line.op === '-' ? 'del' : 'same',
                          children: [
                            jsx('span', { className: 'sb-dno', children: line.a || '' }),
                            jsx('span', { className: 'sb-dno', children: line.b || '' }),
                            jsx('span', { className: 'sb-dop', 'aria-hidden': 'true', children: line.op }),
                            jsx('span', { className: 'sb-dtext', children: line.text || ' ' })
                          ]
                        }, index)))
              })
            ]
          })
        : compare.isError ? null : jsx('div', { className: 'sb-pstage sb-skel-stage', style: { aspectRatio: '32/9' } })
    ]
  })
}

function Lineage({ detail, onOpenShader }) {
  const row = detail.row
  const forks = detail.forks || []

  return jsxs('section', {
    className: 'sb-sec',
    'aria-label': 'Lineage',
    children: [
      jsx('h3', { children: 'Lineage' }),
      jsxs('ul', {
        className: 'sb-tree',
        children: [
          row.parent
            ? jsxs('li', { children: ['Forked from ', jsx('button', { type: 'button', className: 'sb-treelink', 'data-lineage': 'parent', onClick: () => onOpenShader(row.parent.id), children: `${displayName(row.parent.name)}${row.parent.version ? ` v${row.parent.version}` : ''}` })] }, 'parent')
            : jsx('li', { className: 'sb-count', children: row.imported ? `Imported${row.imported.author ? ` from ${row.imported.author}` : ''}` : 'An original, not a fork.' }, 'parent'),
          jsxs('li', {
            className: 'sb-here',
            children: [
              jsx('b', { children: displayName(row.name) }),
              forks.length
                ? jsx('ul', { className: 'sb-tree', children: forks.map(fork => jsx('li', { children: jsx('button', { type: 'button', className: 'sb-treelink', 'data-lineage': 'fork', onClick: () => onOpenShader(fork.id), children: `${displayName(fork.name)}${fork.version ? ` v${fork.version}` : ''}` }) }, fork.id)) })
                : jsx('div', { className: 'sb-count', children: 'No forks yet.' })
            ]
          }, 'here')
        ]
      })
    ]
  })
}

function ShaderSheet({ id, query, archived, onClose, onOpenShader }) {
  const client = sdk.useQueryClient()
  const detailQuery = useShaderDetail(id)
  const detail = detailQuery.data
  const requested = Number(query.get('v')) || 0
  const latestVersion = detail ? detail.latest.version : 0
  const viewingOld = Boolean(detail && requested && requested !== latestVersion)
  const versionQuery = useVersionDoc(id, requested, viewingOld)
  const doc = detail ? (viewingOld ? versionQuery.data : detail.latest) : null
  const ticks = (query.get('compare') || '').split(',').map(Number).filter(n => n > 0).slice(0, 2)
  const [mode, setMode] = useState('')
  const controlRef = useRef(null)
  const closeRef = useRef(null)
  const row = detail ? detail.row : null
  const name = row ? displayName(row.name) : 'Shader'

  useEffect(() => {
    closeRef.current && closeRef.current.focus()
  }, [])

  const openVersion = n => patchPage(query, { v: n === latestVersion ? '' : n })
  const [pending, setPending] = useState('')
  const pendingRef = useRef('')

  // Fork, archive and star wait for their answer; a second click meanwhile does nothing.
  const once = (name, run) => async (...args) => {
    if (pendingRef.current) {
      return
    }

    pendingRef.current = name
    setPending(name)

    try {
      await run(...args)
    } finally {
      pendingRef.current = ''
      setPending('')
    }
  }

  const tick = n => {
    const next = ticks.includes(n) ? ticks.filter(t => t !== n) : [...ticks, n].slice(-2)
    patchPage(query, { compare: next.join(','), tab: next.length === 2 ? 'compare' : '' })
  }

  const fork = once('fork', async () => {
    try {
      const answer = await apiCall(`${shaderPath(id)}/fork`, { method: 'POST', body: { version: doc.version } })
      invalidateShader(client, id)
      const forked = answer && answer.row

      if (forked) {
        say(`Forked v${doc.version} as ${displayName(forked.name)}.`, 'ok', [{ label: 'Open the fork', run: () => onOpenShader(forked.id) }])
      }
    } catch (error) {
      say(`Could not fork: ${problemText(error)}`, 'err')
    }
  })

  const archive = once('archive', async undo => {
    const verb = (undo ? !row.archived : row.archived) ? 'unarchive' : 'archive'

    try {
      await apiCall(`${shaderPath(id)}/${verb}`, { method: 'POST', body: {} })
      invalidateShader(client, id)
      say(verb === 'archive' ? `${name} is archived.` : `${name} is back in My shaders.`, 'ok',
        undo ? null : [{ label: 'Undo', run: () => archive(true) }])
    } catch (error) {
      say(`Could not ${verb}: ${problemText(error)}`, 'err')
    }
  })

  const star = once('star', async () => {
    try {
      await patchShader(client, id, { starred: !row.starred })
    } catch (error) {
      say(`Could not star: ${problemText(error)}`, 'err')
    }
  })

  let body

  if (detailQuery.isError) {
    body = [jsx('p', { className: 'sb-inline-err', role: 'alert', children: problemText(detailQuery.error) }, 'err')]
  } else if (!doc) {
    body = [jsx('div', { className: 'sb-pstage sb-skel-stage' }, 'skel')]
  } else {
    body = [
      jsx(Stage, { doc, label: `${name} v${doc.version}`, values: doc.effectiveValues || doc.tunedValues || null, controlRef }, `${doc.id}@${doc.version}`),
      jsxs('div', {
        className: 'sb-sactions',
        children: [
          jsx('button', { type: 'button', className: 'sb-btn sb-primary', 'data-action': 'continue', onClick: () => continueInChat(row, doc.version), children: [icon('comment-discussion'), 'Continue in chat'] }),
          jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'fork', disabled: Boolean(pending), onClick: fork, children: [icon('repo-forked'), 'Fork'] }),
          jsx(SheetExport, { id, version: doc.version, name: row.name })
        ]
      }, 'actions'),
      jsxs('div', {
        className: 'sb-sactions2',
        children: [
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': 'edit-lab', 'aria-expanded': mode === 'lab' ? 'true' : 'false', onClick: () => setMode(mode === 'lab' ? '' : 'lab'), children: [icon('beaker'), 'Edit in Lab'] }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': 'share', onClick: () => saveShareFiles([{ id, version: doc.version }]), children: [icon('save'), 'Share file'] }),
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': row.archived ? 'unarchive' : 'archive', disabled: Boolean(pending), onClick: () => archive(false), children: [icon(row.archived ? 'discard' : 'archive'), row.archived ? 'Unarchive' : 'Archive'] }),
          jsxs(sdk.DropdownMenu, {
            children: [
              jsx(sdk.DropdownMenuTrigger, { asChild: true, children: jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'aria-label': 'More actions', 'data-action': 'more', children: icon('ellipsis') }) }),
              jsxs(sdk.DropdownMenuContent, {
                align: 'end',
                // The picked form takes the focus, not the trigger.
                onCloseAutoFocus: event => event.preventDefault(),
                children: [
                  jsx(sdk.DropdownMenuItem, { 'data-action': 'rename', onSelect: () => setMode('rename'), children: [icon('edit'), 'Rename'] }),
                  jsx(sdk.DropdownMenuItem, { 'data-action': 'delete', variant: 'destructive', onSelect: () => setMode('delete'), children: [icon('trash'), 'Delete\u2026'] })
                ]
              })
            ]
          })
        ]
      }, 'actions2'),
      mode === 'lab' ? jsx(LabChooser, { row }, 'lab') : null,
      mode === 'rename' ? jsx(RenameForm, { client, row, onDone: () => setMode('') }, 'rename') : null,
      mode === 'delete' ? jsx(DeleteForm, { client, row, onCancel: () => setMode(''), onDeleted: onClose }, 'delete') : null,
      jsxs('dl', {
        className: 'sb-kv',
        children: [
          jsx('dt', { children: 'Checks' }),
          jsx('dd', { children: stateChips(doc) }),
          jsx('dt', { children: 'Licence' }),
          jsx(LicencePicker, { client, detail }),
          jsx('dt', { children: 'Written by' }),
          jsx('dd', { children: `${AUTHOR_WORDS[doc.authorKind] || 'Hermes'}, ${ago(doc.createdAt)}${viewingOld ? `, an older version (latest is v${latestVersion})` : ''}` }),
          row.imported ? jsx('dt', { children: 'Source' }) : null,
          row.imported ? jsx('dd', { children: [row.imported.author || 'unknown author', row.imported.url ? jsx('span', { className: 'sb-count', children: row.imported.url }) : null] }) : null,
          jsx('dt', { children: 'Tags' }),
          jsx(TagEditor, { client, row })
        ]
      }, 'kv'),
      jsx(SheetSliders, { id, doc, controlRef, live: !viewingOld }, `sliders-${doc.version}`),
      jsx(Filmstrip, { detail, current: doc.version, ticks, onOpen: openVersion, onTick: tick }, 'film'),
      ticks.length === 2 ? jsx(CompareView, { id, a: ticks[0], b: ticks[1], onClose: () => patchPage(query, { compare: '', tab: '' }) }, `cmp-${ticks.join(',')}`) : null,
      jsx(Lineage, { detail, onOpenShader }, 'lineage')
    ]
  }

  return jsxs('aside', {
    className: 'sb-sheet',
    role: 'dialog',
    'aria-modal': 'false',
    'aria-labelledby': 'sb-sheet-title',
    'data-sheet': id,
    children: [
      jsxs('header', {
        className: 'sb-shead',
        children: [
          jsx('h2', { id: 'sb-sheet-title', title: name, children: name }),
          doc ? jsx('span', { className: 'sb-ver', children: `v${doc.version}` }) : null,
          jsx('span', { className: 'sb-grow' }),
          row
            ? jsx('button', { type: 'button', className: 'sb-icon-btn', 'aria-pressed': row.starred ? 'true' : 'false', 'aria-label': row.starred ? 'Unstar' : 'Star', 'data-action': 'star', disabled: Boolean(pending), onClick: star,
                children: icon(row.starred ? 'star-full' : 'star-empty', row.starred ? 'sb-star-on' : '') })
            : null,
          jsx('button', { type: 'button', ref: closeRef, className: 'sb-icon-btn', 'aria-label': 'Close (Escape)', 'data-action': 'close-sheet', onClick: onClose, children: icon('close') })
        ]
      }),
      jsx('div', { className: 'sb-sbody', children: body })
    ]
  })
}

const IMPORT_LICENCES = [
  ['', 'Unknown or none'], ['CC0-1.0', 'CC0'], ['MIT', 'MIT'], ['CC-BY-4.0', 'CC BY 4.0'], ['CC-BY-SA-4.0', 'CC BY-SA 4.0'],
  ['CC-BY-NC-4.0', 'CC BY-NC 4.0'], ['CC-BY-NC-SA-3.0', 'CC BY-NC-SA 3.0 (Shadertoy default)'], ['CC-BY-NC-SA-4.0', 'CC BY-NC-SA 4.0']
]

/** Import a share file (picked with ctx.files, or pasted) or a pasted Shadertoy, glslCanvas or ISF source. */
function ImportPanel({ onClose, onOpenShader }) {
  const client = sdk.useQueryClient()
  const [tab, setTab] = useState('file')
  const [folder, setFolder] = useState(null)
  const [entries, setEntries] = useState([])
  const [shareText, setShareText] = useState('')
  const [form, setForm] = useState({ source: '', name: '', url: '', author: '', licence: '' })
  const [state, setState] = useState({ busy: false, result: null, error: '', diagnostics: [] })
  const closeRef = useRef(null)

  useEffect(() => {
    closeRef.current && closeRef.current.focus()
  }, [])

  const send = async body => {
    setState({ busy: true, result: null, error: '', diagnostics: [] })

    try {
      const answer = await apiCall('/import', { method: 'POST', body, timeoutMs: LONG_MS })

      if (answer && answer.ok) {
        invalidateShader(client)
        setState({ busy: false, result: answer, error: '', diagnostics: [] })
      } else {
        setState({ busy: false, result: null, error: (answer && answer.message) || 'The import did not pass the checks.', diagnostics: (answer && answer.diagnostics) || [] })
      }
    } catch (error) {
      const problem = restProblem(error)
      setState({ busy: false, result: null, error: problem.code === 'busy' ? 'The renderer is busy with other renders. Try again in a moment.' : problemText(error), diagnostics: [] })
    }
  }

  const fileError = (what, error) => setState({ busy: false, result: null, error: `${what}: ${(error && error.message) || 'unknown error'}`, diagnostics: [] })

  const pickFolder = async () => {
    let picked = null
    let listing = null

    try {
      picked = await pluginFiles.requestFolder()

      if (!picked || !picked.ok || !picked.folder) {
        return
      }

      listing = await pluginFiles.list(picked.folder.id, '')
    } catch (error) {
      fileError('Could not open the folder', error)

      return
    }

    setFolder(picked.folder)
    setEntries(listing && listing.ok ? listing.entries.filter(entry => !entry.isDirectory && entry.name.endsWith('.shaderbruecke.json')) : [])
  }

  const importFile = async entry => {
    let read = null

    try {
      read = await pluginFiles.readText(folder.id, entry.path)
    } catch (error) {
      fileError(`Could not read ${entry.name}`, error)

      return
    }

    if (!read || !read.ok) {
      setState({ busy: false, result: null, error: `Could not read ${entry.name}: ${(read && read.message) || 'unknown error'}`, diagnostics: [] })

      return
    }

    importShareText(read.text, entry.name)
  }

  const importShareText = (text, label) => {
    let share = null

    try {
      share = JSON.parse(text)
    } catch {
      setState({ busy: false, result: null, error: `${label || 'The pasted text'} is not JSON.`, diagnostics: [] })

      return
    }

    if (!share || share.format !== 'shaderbruecke/1') {
      setState({ busy: false, result: null, error: `${label || 'The pasted text'} is not a shaderbruecke/1 share file.`, diagnostics: [] })

      return
    }

    send({ share })
  }

  const field = (key, label, props = {}) => jsxs('label', {
    className: 'sb-form-row',
    children: [jsx('span', { children: label }), jsx(props.multiline ? 'textarea' : 'input', {
      className: 'sb-field',
      name: key,
      value: form[key],
      rows: props.multiline ? 9 : undefined,
      placeholder: props.placeholder,
      spellCheck: false,
      onChange: event => setForm(current => ({ ...current, [key]: event.target.value }))
    })]
  })

  const result = state.result

  return jsxs('aside', {
    className: 'sb-sheet',
    role: 'dialog',
    'aria-modal': 'false',
    'aria-labelledby': 'sb-import-title',
    'data-import': 'true',
    children: [
      jsxs('header', {
        className: 'sb-shead',
        children: [
          jsx('h2', { id: 'sb-import-title', children: 'Import a shader' }),
          jsx('span', { className: 'sb-grow' }),
          jsx('button', { type: 'button', ref: closeRef, className: 'sb-icon-btn', 'aria-label': 'Close (Escape)', onClick: onClose, children: icon('close') })
        ]
      }),
      jsxs('div', {
        className: 'sb-sbody',
        children: [
          jsx('p', { className: 'sb-count', style: { marginTop: 0 }, children: 'Every import is linted and rendered on the Hermes host before it lands. Nothing is fetched from the web: author, link and licence are kept as you enter them.' }),
          jsxs('div', {
            className: 'sb-seg',
            role: 'group',
            'aria-label': 'Import from',
            style: { marginBottom: 12 },
            children: [
              jsx('button', { type: 'button', 'aria-pressed': tab === 'file' ? 'true' : 'false', 'data-import-tab': 'file', onClick: () => setTab('file'), children: 'Share file' }),
              jsx('button', { type: 'button', 'aria-pressed': tab === 'paste' ? 'true' : 'false', 'data-import-tab': 'paste', onClick: () => setTab('paste'), children: 'Paste source' })
            ]
          }),
          tab === 'file'
            ? pluginFiles
              ? jsxs('div', {
                  children: [
                    jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'pick-folder', onClick: pickFolder, children: [icon('folder-opened'), folder ? `Pick another folder` : 'Pick a folder'] }),
                    folder
                      ? jsx('ul', {
                          className: 'sb-files',
                          'aria-label': `Share files in ${folder.name}`,
                          children: entries.length
                            ? entries.map(entry => jsxs('li', { children: [jsx('code', { children: entry.name }), jsx('button', { type: 'button', className: 'sb-btn', disabled: state.busy, 'data-import-file': entry.name, onClick: () => importFile(entry), children: 'Import' })] }, entry.path))
                            : jsx('li', { className: 'sb-count', children: `No .shaderbruecke.json files in ${folder.name}.` })
                        })
                      : null
                  ]
                })
              : jsxs('div', {
                  className: 'sb-form',
                  children: [
                    jsx('p', { className: 'sb-note', children: 'This Desktop cannot open files for plugins. Paste the share file\'s JSON instead.' }),
                    jsx('textarea', { className: 'sb-field', rows: 8, value: shareText, 'aria-label': 'Share file JSON', onChange: event => setShareText(event.target.value) }),
                    jsx('button', { type: 'button', className: 'sb-btn', disabled: !shareText.trim() || state.busy, onClick: () => importShareText(shareText), children: 'Import' })
                  ]
                })
            : jsxs('form', {
                className: 'sb-form',
                onSubmit: event => {
                  event.preventDefault()
                  send({ source: form.source, name: form.name || undefined, url: form.url || undefined, author: form.author || undefined, licence: form.licence || undefined })
                },
                children: [
                  field('source', 'Source (Shadertoy Image tab, glslCanvas or ISF)', { multiline: true, placeholder: 'void mainImage(out vec4 fragColor, in vec2 fragCoord) { ... }' }),
                  field('name', 'Name'),
                  field('url', 'Where it came from (link)', { placeholder: 'https://www.shadertoy.com/view/...' }),
                  field('author', 'Author'),
                  jsxs('label', {
                    className: 'sb-form-row',
                    children: [jsx('span', { children: 'Licence of the source' }), jsx('select', {
                      className: 'sb-field',
                      name: 'licence',
                      value: form.licence,
                      onChange: event => setForm(current => ({ ...current, licence: event.target.value })),
                      children: IMPORT_LICENCES.map(([value, text]) => jsx('option', { value, children: text }, value))
                    })]
                  }),
                  /NC/.test(form.licence) ? jsx('p', { className: 'sb-note', 'data-nc': 'true', children: 'Non-commercial: the imported shader and every fork keep this licence, and it shows in every export header.' }) : null,
                  jsx('button', { type: 'submit', className: 'sb-btn sb-primary', 'data-action': 'import-paste', disabled: !form.source.trim() || state.busy, children: state.busy ? [icon('loading', 'codicon-modifier-spin'), 'Rendering'] : 'Import and render' })
                ]
              }),
          state.busy && tab === 'file' ? jsx('p', { className: 'sb-count', children: 'Rendering on the Hermes host. This can take a minute.' }) : null,
          state.error
            ? jsxs('div', {
                className: 'sb-inline-err',
                role: 'alert',
                'data-import-error': 'true',
                children: [jsx('b', { children: state.error }), state.diagnostics.length
                  ? jsx('ul', { children: state.diagnostics.map((d, i) => jsx('li', { children: `Line ${d.line}: ${d.message}` }, i)) })
                  : null]
              })
            : null,
          result
            ? jsxs('div', {
                className: 'sb-banner',
                'data-import-ok': 'true',
                style: { marginTop: 12 },
                children: [
                  icon('check', 'sb-ok'),
                  jsxs('div', {
                    children: [
                      jsx('b', { children: `Imported ${displayName(result.row.name)}` }),
                      result.warnings && result.warnings.length ? jsx('ul', { children: result.warnings.map((w, i) => jsx('li', { children: w }, i)) }) : null
                    ]
                  }),
                  jsx('div', { className: 'sb-banner-actions', children: jsx('button', { type: 'button', className: 'sb-btn', onClick: () => onOpenShader(result.row.id), children: 'Open' }) })
                ]
              })
            : null
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------------------------
// The Lab (F40): edit a shader's code beside the chat. Opened with host.openWorkspace docked right
// or centre (the choice is remembered in ctx.storage 'labDock'); on a Desktop without
// openWorkspace it is a tab of the Shaders page. Unsaved text lives in ctx.storage 'lab:<id>'
// ({text, base, savedAt}) until Save makes a new version through the server check, Discard edits
// drops it, or the shader is deleted.
// ---------------------------------------------------------------------------------------------

const LAB_LINE_PX = 20
const LAB_PAD_PX = 8
const LAB_RECOMPILE_MS = 300
const LEARN_RE = /^\s*\/\/\s*(?:\?|learn:)\s?(.*)$/i
const GL_ERROR_RE = /(ERROR|WARNING):\s*(\d+):(\d+):\s*([^\n]*)/g

/** Compiler log lines of the shader's own source string (the header sets `#line 1 1`). */
function compileMarkers(log) {
  const out = []
  let match = GL_ERROR_RE.exec(log || '')

  while (match) {
    if (match[2] === '1') {
      out.push({ line: Number(match[3]), severity: match[1] === 'ERROR' ? 'error' : 'warn', message: match[4].trim(), source: 'gpu' })
    }

    match = GL_ERROR_RE.exec(log || '')
  }

  GL_ERROR_RE.lastIndex = 0

  return out
}

/** `// ?` and `// learn:` lines, each tied to the first slider its text names. */
function learnNotes(text, sliders) {
  const notes = []

  String(text || '').split('\n').forEach((line, index) => {
    const match = LEARN_RE.exec(line)

    if (match && match[1].trim()) {
      const slider = sliders.find(s => new RegExp(`\\b${s.name}\\b`).test(match[1])) || null
      notes.push({ line: index + 1, text: match[1].trim(), slider })
    }
  })

  return notes
}

function labKey(id) {
  return `lab:${id}`
}

/** Open the Lab for a shader: a workspace tab docked `pos` ('right' or 'center'), else the page tab. */
function openLab(row, pos) {
  storeSet('labDock', pos)
  const host = sdk.host || {}

  if (typeof host.openWorkspace === 'function') {
    try {
      host.openWorkspace(`shaderbruecke-lab-${row.id}`, {
        title: `Lab: ${displayName(row.name)}`,
        dock: { pane: 'workspace', pos },
        minWidth: '26rem',
        render: () => jsx(LabRoot, { id: row.id }),
        onClose: () => {}
      })

      return 'workspace'
    } catch {
      // Fall back to the page tab below.
    }
  }

  goPage({ lab: row.id })

  return 'page'
}

function LabChooser({ row }) {
  const remembered = storeGet('labDock', 'right')
  const docks = typeof (sdk.host || {}).openWorkspace === 'function'
  const button = (pos, label, glyph) => jsx('button', {
    type: 'button',
    className: `sb-btn${remembered === pos ? ' sb-btn-on' : ''}`,
    'data-action': `lab-${pos}`,
    title: remembered === pos ? 'Your last choice' : undefined,
    onClick: () => openLab(row, pos),
    children: [icon(glyph), label]
  }, pos)

  return jsxs('div', {
    className: 'sb-lab-choose',
    role: 'group',
    'aria-label': 'Open the Lab',
    children: docks
      ? [button('right', 'Open Lab right', 'layout-sidebar-right'), button('center', 'Open Lab center', 'layout-centered'),
          jsx('span', { className: 'sb-count', children: 'Right keeps the chat visible; center opens a tab beside it.' }, 'hint')]
      : [jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'lab-page', onClick: () => openLab(row, 'center'), children: [icon('beaker'), 'Open Lab here'] }, 'page'),
          jsx('span', { className: 'sb-count', children: 'This Desktop opens the Lab as a tab of this page.' }, 'hint')]
  })
}

function CodeEditor({ text, onChange, markers, onSave, editorRef, label }) {
  const gutterRef = useRef(null)
  const backRef = useRef(null)
  const escaped = useRef(false)
  const lines = text.split('\n').length
  const byLine = new Map()

  for (const marker of markers) {
    const prior = byLine.get(marker.line)

    if (!prior || (prior.severity !== 'error' && marker.severity === 'error')) {
      byLine.set(marker.line, marker)
    }
  }

  const sync = () => {
    const area = editorRef.current

    if (!area) {
      return
    }

    gutterRef.current && (gutterRef.current.scrollTop = area.scrollTop)
    backRef.current && (backRef.current.style.transform = `translate(${-area.scrollLeft}px, ${-area.scrollTop}px)`)
  }

  const onKeyDown = event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault()
      onSave()

      return
    }

    if (event.key === 'Escape') {
      // Escape, then Tab, leaves the editor; Tab alone indents.
      escaped.current = true

      return
    }

    if (event.key === 'Tab' && !escaped.current && !event.shiftKey) {
      event.preventDefault()
      const area = event.currentTarget
      let inserted = false

      // insertText goes through the browser's edit stack, so Ctrl or Cmd and Z undoes it.
      try {
        inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, '  ')
      } catch {
        inserted = false
      }

      if (!inserted) {
        const start = area.selectionStart
        onChange(`${area.value.slice(0, start)}  ${area.value.slice(area.selectionEnd)}`)
        window.requestAnimationFrame(() => {
          area.selectionStart = area.selectionEnd = start + 2
        })
      }

      return
    }

    escaped.current = false
  }

  return jsxs('div', {
    className: 'sb-ed',
    children: [
      jsx('div', {
        className: 'sb-ed-gutter',
        ref: gutterRef,
        'aria-hidden': 'true',
        children: Array.from({ length: lines }, (_, i) => {
          const marker = byLine.get(i + 1)

          return jsx('div', { 'data-line': i + 1, 'data-mark': marker ? marker.severity : undefined, title: marker ? marker.message : undefined, children: i + 1 }, i)
        })
      }),
      jsxs('div', {
        className: 'sb-ed-main',
        children: [
          jsx('div', {
            className: 'sb-ed-back',
            ref: backRef,
            'aria-hidden': 'true',
            children: [...byLine.values()].map(marker => jsx('div', {
              className: 'sb-ed-bar',
              'data-mark': marker.severity,
              style: { top: `${LAB_PAD_PX + (marker.line - 1) * LAB_LINE_PX}px` }
            }, marker.line))
          }),
          jsx('textarea', {
            ref: editorRef,
            className: 'sb-ed-text',
            value: text,
            wrap: 'off',
            spellCheck: false,
            autoCapitalize: 'off',
            autoComplete: 'off',
            'aria-label': label,
            'aria-describedby': 'sb-lab-markers',
            onChange: event => onChange(event.target.value),
            onScroll: sync,
            onKeyDown
          })
        ]
      })
    ]
  })
}

function moveCaretToLine(area, line) {
  if (!area) {
    return
  }

  const lines = area.value.split('\n')
  let offset = 0

  for (let i = 0; i < Math.min(line - 1, lines.length); i++) {
    offset += lines[i].length + 1
  }

  area.focus()
  area.selectionStart = area.selectionEnd = offset
  area.scrollTop = Math.max(0, (line - 4) * LAB_LINE_PX)
}

function LabRoot({ id, onBack }) {
  return jsxs('div', {
    className: 'sb-lab',
    'data-lab': id,
    children: [jsx(LabEditor, { id, onBack }), jsx(Toaster, { scope: `lab:${id}` })]
  })
}

/** An edit has not been through the server's flash check, so its preview waits for a click. */
const LAB_UNCHECKED_GATE = { level: 'unchecked', word: 'Not checked yet', tone: 'warn', glyph: 'shield',
  text: 'Edits pass the flash check when you save, so this preview waits for your click.' }

/** The same gate on a shader whose saved version plays by itself: its Play click is stored (`labPlay`). */
const LAB_FIRST_PLAY_GATE = { ...LAB_UNCHECKED_GATE, remember: true,
  text: 'Edits are flash-checked only when you save. Play starts them live, in this Lab and every later one.' }

const SAVE_KEYS = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '') ? '\u2318S' : 'Ctrl+S'

function LabEditor({ id, onBack }) {
  const client = sdk.useQueryClient()
  const detailQuery = useShaderDetail(id)
  const detail = detailQuery.data
  const latest = detail ? detail.latest : null
  const scope = `lab:${id}`
  const [draft, setDraft] = useState(null)
  // The text and the sliders the preview runs, always taken together after the recompile delay:
  // sliders from the live draft would rebuild the player with uniforms the program lacks.
  const [compiled, setCompiled] = useState({ text: '', sliders: [] })
  // Play consent for this Lab: asked once for unchecked text, then kept across recompiles.
  const [consent, setConsent] = useState(false)
  // The version a save answered, standing in for `latest` until the detail refetch carries it.
  const [answered, setAnswered] = useState(null)
  const [lint, setLint] = useState([])
  const [gpu, setGpu] = useState([])
  const [server, setServer] = useState({ markers: [], message: '' })
  const [stale, setStale] = useState(null)
  const [saving, setSaving] = useState(false)
  const [learnOpen, setLearnOpen] = useState(true)
  const [values, setValues] = useState({})
  const editorRef = useRef(null)
  const controlRef = useRef(null)
  // The text and version last known to be stored on the server; equal means nothing to keep.
  const savedRef = useRef(null)

  // The draft starts from the stored unsaved text, else from the latest version.
  useEffect(() => {
    if (!latest || draft) {
      return
    }

    savedRef.current = { text: latest.source, base: latest.version }
    const saved = storeGet(labKey(id), null)
    const start = saved && typeof saved.text === 'string' ? saved : { text: latest.source, base: latest.version }
    const restored = Boolean(saved && saved.text !== latest.source)
    setDraft({ text: start.text, base: Number(start.base) || latest.version, restored, restoredAt: restored ? Number(saved.savedAt) || 0 : 0 })
  }, [latest && latest.version])

  const text = draft ? draft.text : ''
  const sliders = useMemo(() => parseSliders(text), [text])

  useEffect(() => {
    if (!draft) {
      return undefined
    }

    const timer = window.setTimeout(() => {
      const findings = lintSource(draft.text, sliders.map(slider => slider.name))
      setLint(findings.map(f => ({ line: f.line, severity: f.severity === 'error' ? 'error' : 'warn', message: `${capitalize(f.message)}. ${capitalize(f.hint)}.`, source: 'lint' })))

      if (!findings.some(f => f.severity === 'error')) {
        setGpu([])
        setCompiled(current => (current.text === draft.text ? current : { text: draft.text, sliders }))
      }

      if (savedRef.current && draft.text === savedRef.current.text && draft.base === savedRef.current.base) {
        storeRemove(labKey(id))
      } else {
        storeSet(labKey(id), { text: draft.text, base: draft.base, savedAt: Date.now() })
      }
    }, LAB_RECOMPILE_MS)

    return () => window.clearTimeout(timer)
  }, [draft && draft.text, draft && draft.base])

  const previewSignature = sliderSignature(compiled.sliders)

  useEffect(() => {
    setValues(current => startingValues(compiled.sliders, { ...(latest ? latest.effectiveValues : {}), ...current }))
  }, [previewSignature])

  const setText = next => {
    setDraft(current => ({ ...current, text: next }))
    setServer({ markers: [], message: '' })
  }

  const onPhase = state => {
    if (state.phase === 'error') {
      const markers = compileMarkers(state.log)
      setGpu(markers.length ? markers : [{ line: 1, severity: 'error', message: (state.log || 'compile failed').split('\n')[0], source: 'gpu' }])
    } else if (state.phase === 'live' || state.phase === 'still' || state.phase === 'paused') {
      setGpu([])
    }
  }

  const save = async () => {
    if (!draft || saving || (savedRef.current && draft.text === savedRef.current.text && draft.base === savedRef.current.base)) {
      return
    }

    // Lint the text as it is now (Ctrl or Cmd and S can beat the recompile delay); the GPU result
    // counts only when it belongs to this text. Warnings never block.
    const lintErrors = lintSource(draft.text, sliders.map(slider => slider.name)).some(f => f.severity === 'error')
    const gpuErrors = compiled.text === draft.text && gpu.some(m => m.severity === 'error')

    if (lintErrors || gpuErrors) {
      say('Fix the marked lines first; the server would refuse them too.', 'warn', null, scope)

      return
    }

    setSaving(true)
    setStale(null)

    try {
      const answer = await apiCall(`${shaderPath(id)}/versions`, { method: 'POST', body: { source: draft.text, base_version: draft.base }, timeoutMs: LONG_MS })

      if (answer && answer.ok) {
        const held = playGate(answer.version)
        savedRef.current = { text: draft.text, base: answer.version.version }
        storeRemove(labKey(id))
        setAnswered(answer.version)
        setDraft({ text: draft.text, base: answer.version.version, restored: false })
        setServer({ markers: [], message: '' })

        if (held) {
          setConsent(false)
        }

        invalidateShader(client, id)
        say(held
          ? `Saved as v${answer.version.version}. ${held.text} Hermes sees it in your next message.`
          : `Saved as v${answer.version.version}. Hermes sees it in your next message.`, held ? 'warn' : 'ok', null, scope)
      } else {
        const markers = ((answer && answer.diagnostics) || []).map(d => ({ line: d.line, severity: d.severity === 'warning' ? 'warn' : 'error', message: d.message, source: 'server' }))
        setServer({ markers, message: (answer && answer.message) || 'The server check refused this version.' })
      }
    } catch (error) {
      const problem = restProblem(error)

      if (problem.code === 'stale_base') {
        setStale({ latest: Number(problem.detail.latest) || null })
      } else if (problem.code === 'busy') {
        say('The renderer is busy with other renders. Try again in a moment.', 'warn', null, scope)
      } else if (problem.code === 'not_found') {
        storeRemove(labKey(id))
        say(`Not saved: ${problem.message || 'This shader was deleted.'}`, 'err', null, scope)
      } else {
        say(`Not saved: ${problemText(error)}`, 'err', null, scope)
      }
    } finally {
      setSaving(false)
    }
  }

  const loadLatest = async () => {
    let fresh = null

    try {
      fresh = await client.fetchQuery({ queryKey: qkey('detail', id), queryFn: () => apiCall(shaderPath(id)), staleTime: 0 })
    } catch (error) {
      say(`Could not load the latest version: ${problemText(error)}`, 'err', null, scope)

      return
    }

    savedRef.current = { text: fresh.latest.source, base: fresh.latest.version }
    storeRemove(labKey(id))
    setDraft({ text: fresh.latest.source, base: fresh.latest.version, restored: false })
    setStale(null)
    say(`Loaded v${fresh.latest.version}. Your edits are gone.`, 'none', null, scope)
  }

  const keepEditing = () => {
    setDraft(current => ({ ...current, base: stale.latest || current.base }))
    setStale(null)
    say(`Kept your text. Save now makes a new version after v${stale.latest}.`, 'none', null, scope)
  }

  const discard = () => {
    const saved = savedRef.current

    if (!saved) {
      return
    }

    storeRemove(labKey(id))
    setDraft({ text: saved.text, base: saved.base, restored: false })
    setServer({ markers: [], message: '' })
    setStale(null)
    say(`Edits discarded; back to v${saved.base}.`, 'none', null, scope)
  }

  if (detailQuery.isError) {
    return jsx('p', { className: 'sb-inline-err', role: 'alert', style: { padding: 16 }, children: problemText(detailQuery.error) })
  }

  if (!detail || !draft) {
    return jsx('div', { className: 'sb-lab-body', children: jsx('div', { className: 'sb-pstage sb-skel-stage', style: { margin: 16 } }) })
  }

  const markers = [...lint, ...gpu, ...server.markers].sort((a, b) => a.line - b.line)
  const errors = markers.filter(m => m.severity === 'error')
  const dirty = !savedRef.current || draft.text !== savedRef.current.text || draft.base !== savedRef.current.base
  const notes = learnNotes(draft.text, sliders)
  const name = displayName(detail.row.name)
  const status = errors.length ? `${errors.length} ${errors.length === 1 ? 'problem' : 'problems'}` : dirty ? 'Unsaved changes' : 'Saved'
  const previewDoc = { id, version: draft.base, source: compiled.text, sliders: compiled.sliders, thumb: latest.thumb }
  // The saved text plays by the server's verdict. Other text is unchecked: on a shader whose saved
  // version plays by itself one stored Play click lets it run; any other verdict keeps a gate in
  // every Lab, block its stronger one, because Stage arms with `consent || !gate`.
  const savedVersion = answered && answered.version > latest.version ? answered : latest
  const savedGate = playGate(savedVersion)
  const labPlay = storeGet('labPlay', false) === true
  const editGate = savedGate ? (savedGate.level === 'block' ? savedGate : LAB_UNCHECKED_GATE) : labPlay ? null : LAB_FIRST_PLAY_GATE
  const previewGate = savedRef.current && compiled.text === savedRef.current.text ? savedGate : editGate
  const onConsent = () => {
    if (previewGate && previewGate.remember) {
      storeSet('labPlay', true)
    }

    setConsent(true)
  }

  return jsxs('div', {
    className: 'sb-lab-frame',
    children: [
      jsxs('header', {
        className: 'sb-lab-head',
        children: [
          onBack ? jsx('button', { type: 'button', className: 'sb-icon-btn', 'aria-label': 'Back to Shaderbrücke', onClick: onBack, children: icon('arrow-left') }) : null,
          icon('beaker'),
          jsx('b', { className: 'sb-lab-name', children: name }),
          jsx('span', { className: 'sb-ver', children: `based on v${draft.base}` }),
          jsx('span', { className: `sb-lab-status${errors.length ? ' sb-err' : ''}`, role: 'status', 'data-status': errors.length ? 'problems' : dirty ? 'dirty' : 'saved', children: status }),
          jsx('span', { className: 'sb-grow' }),
          dirty && savedRef.current
            ? jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': 'lab-discard', disabled: saving, onClick: discard, children: [icon('discard'), 'Discard edits'] })
            : null,
          jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'aria-pressed': learnOpen ? 'true' : 'false', 'data-action': 'learn', onClick: () => setLearnOpen(v => !v), children: [icon('lightbulb'), 'Learn'] }),
          jsx('button', { type: 'button', className: 'sb-btn sb-primary', 'data-action': 'lab-save', disabled: saving || !dirty, onClick: save, title: `Save version (${SAVE_KEYS})`,
            children: saving ? [icon('loading', 'codicon-modifier-spin'), 'Checking'] : [icon('save'), 'Save version', jsx('span', { className: 'sb-kbd', 'aria-hidden': 'true', children: SAVE_KEYS })] })
        ]
      }),
      draft.restored
        ? jsx('p', { className: 'sb-lab-note', children: `Unsaved edits from ${draft.restoredAt ? ago(new Date(draft.restoredAt).toISOString()) : 'before'}, based on v${draft.base}. Save makes them a version; Discard edits drops them.` })
        : null,
      stale
        ? jsxs('div', {
            className: 'sb-banner sb-lab-banner',
            role: 'alert',
            'data-stale': 'true',
            children: [
              icon('warning', 'sb-warn'),
              jsxs('div', { children: [jsx('b', { children: 'A newer version exists' }), jsx('p', { children: `v${stale.latest || '?'} was saved after you started from v${draft.base}.` })] }),
              jsxs('div', {
                className: 'sb-banner-actions',
                children: [
                  jsx('button', { type: 'button', className: 'sb-btn', 'data-action': 'load-latest', onClick: loadLatest, children: `Load v${stale.latest || 'latest'}` }),
                  jsx('button', { type: 'button', className: 'sb-btn sb-ghost', 'data-action': 'keep-editing', onClick: keepEditing, children: 'Keep editing' })
                ]
              })
            ]
          })
        : null,
      server.message
        ? jsxs('div', { className: 'sb-banner sb-lab-banner', role: 'alert', 'data-server-error': 'true', children: [icon('error', 'sb-err'), jsx('div', { children: [jsx('b', { children: 'Not saved' }), jsx('p', { children: server.message })] })] })
        : null,
      jsxs('div', {
        className: 'sb-lab-body',
        children: [
          jsxs('section', {
            className: 'sb-lab-code',
            'aria-label': 'Code',
            children: [
              jsx(CodeEditor, { text: draft.text, onChange: setText, markers, onSave: save, editorRef, label: `Source of ${name}. Tab indents; Escape then Tab leaves the editor; Ctrl or Cmd and S saves.` }),
              jsx('ul', {
                id: 'sb-lab-markers',
                className: 'sb-lab-markers',
                'aria-label': 'Problems',
                children: markers.length
                  ? markers.map((m, i) => jsx('li', {
                      'data-mark': m.severity,
                      children: jsxs('button', {
                        type: 'button',
                        onClick: () => moveCaretToLine(editorRef.current, m.line),
                        children: [icon(m.severity === 'error' ? 'error' : 'warning', m.severity === 'error' ? 'sb-err' : 'sb-warn'), jsx('b', { children: `Line ${m.line}` }), ` ${m.message}`]
                      })
                    }, `${m.source}-${m.line}-${i}`))
                  : jsx('li', { className: 'sb-count', children: 'No problems. Every change is linted and compiled here as you type.' })
              })
            ]
          }),
          jsxs('aside', {
            className: 'sb-lab-side',
            'aria-label': 'Preview',
            children: [
              compiled.text
                ? jsx(Stage, { doc: previewDoc, label: `${name}, Lab preview`, values, controlRef, onPhaseChange: onPhase, gate: previewGate, consent, onConsent }, 'preview')
                : jsx('div', { className: 'sb-pstage sb-skel-stage' }),
              compiled.sliders.length
                ? jsxs('section', {
                    className: 'sb-sec',
                    'aria-label': 'Sliders',
                    children: [jsx('h3', { children: 'Sliders (this preview only)' }), ...compiled.sliders.map(slider => jsx(SliderRow, {
                      slider,
                      value: values[slider.name] ?? slider.default,
                      onChange: (n, v) => {
                        setValues(current => ({ ...current, [n]: v }))
                        controlRef.current && controlRef.current.setSlider(n, v)
                      },
                      onBlur: () => {},
                      onReset: () => {
                        setValues(current => ({ ...current, [slider.name]: slider.default }))
                        controlRef.current && controlRef.current.setSlider(slider.name, slider.default)
                      }
                    }, slider.name))]
                  })
                : null,
              learnOpen
                ? jsxs('section', {
                    className: 'sb-sec sb-learn',
                    'aria-label': 'Learn',
                    children: [
                      jsx('h3', { children: 'Learn' }),
                      jsx('p', { className: 'sb-note', children: 'Reads the comments that start with // ? or // learn: and shows each beside its line; a note that names a slider gets that slider.' }),
                      notes.length
                        ? jsx('ol', {
                            className: 'sb-notes',
                            children: notes.map(note => jsxs('li', {
                              'data-note-line': note.line,
                              children: [
                                jsx('button', { type: 'button', className: 'sb-link', onClick: () => moveCaretToLine(editorRef.current, note.line), children: `Line ${note.line}` }),
                                jsx('p', { children: note.text }),
                                note.slider
                                  ? jsx(SliderRow, {
                                      slider: note.slider,
                                      value: values[note.slider.name] ?? note.slider.default,
                                      onChange: (n, v) => {
                                        setValues(current => ({ ...current, [n]: v }))
                                        controlRef.current && controlRef.current.setSlider(n, v)
                                      },
                                      onBlur: () => {},
                                      onReset: () => {}
                                    })
                                  : null
                              ]
                            }, note.line))
                          })
                        : jsx('p', { className: 'sb-count', children: 'No notes in this source yet. Ask Hermes to explain the shader with // learn: comments.' })
                    ]
                  })
                : null
            ]
          })
        ]
      })
    ]
  })
}

const PAGE_CSS = `
.sb-shell{position:relative;height:100%;min-height:0;overflow:hidden;font-size:13px;line-height:1.45;color:var(--ui-text-primary)}
.sb-shell *{box-sizing:border-box}
.sb-shell :focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.sb-page{position:absolute;inset:0;overflow:auto;padding:22px 28px 72px}
.sb-wrap{max-width:1320px;margin:0 auto}
.sb-phead{display:flex;align-items:baseline;gap:12px;margin-bottom:10px}
.sb-phead h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-.01em}
.sb-phead .sb-btn{align-self:center}
.sb-count{color:var(--ui-text-secondary);font-size:12.5px}
.sb-grow{flex:1}
.sb-tabs{display:flex;gap:2px;border-bottom:1px solid var(--ui-stroke-tertiary);margin-bottom:12px}
.sb-tab{border:0;background:none;height:32px;padding:0 10px;cursor:pointer;color:var(--ui-text-secondary);font:inherit;font-size:13px;border-radius:6px 6px 0 0}
.sb-tab:hover{color:var(--ui-text-primary)}
.sb-tab[aria-selected=true]{color:var(--ui-text-primary);font-weight:600;box-shadow:inset 0 -2px 0 var(--ui-accent)}
.sb-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px;position:relative}
.sb-search{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 8px;border-radius:8px;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary);min-width:230px}
.sb-search:focus-within{border-color:var(--ui-accent)}
.sb-search input{border:0;background:none;outline:none;color:var(--ui-text-primary);font:inherit;font-size:12.5px;width:100%;min-width:0}
.sb-search input::placeholder{color:var(--ui-text-secondary)}
.sb-field{height:28px;border-radius:8px;border:1px solid var(--ui-stroke-secondary);background:transparent;color:var(--ui-text-primary);font:inherit;font-size:12.5px;padding:0 8px;min-width:0}
.sb-field:focus{border-color:var(--ui-accent);outline:none}
.sb-field option{background:var(--ui-bg-elevated);color:var(--ui-text-primary)}
textarea.sb-field,input.sb-field{background:transparent}
textarea.sb-field{height:auto;padding:7px 9px;resize:vertical;font:12px/1.5 var(--dt-font-mono)}
.sb-seg{display:inline-flex;padding:2px;border-radius:8px;background:var(--ui-bg-quaternary);gap:2px}
.sb-seg button{border:0;background:none;height:24px;padding:0 8px;border-radius:6px;cursor:pointer;color:var(--ui-text-secondary);display:inline-flex;align-items:center;gap:5px;font:inherit;font-size:12.5px}
.sb-seg button[aria-pressed=true]{background:var(--ui-bg-elevated);color:var(--ui-text-primary);box-shadow:0 1px 2px rgb(0 0 0/.12)}
.sb-pop{position:absolute;top:34px;left:238px;z-index:30;width:280px;padding:12px;border-radius:10px;background:var(--ui-bg-elevated);border:1px solid var(--ui-stroke-secondary);box-shadow:0 14px 36px rgb(0 0 0/.3),0 2px 6px rgb(0 0 0/.12);display:grid;gap:10px}
.sb-pop label{display:grid;gap:4px;font-size:12px;color:var(--ui-text-secondary)}
.sb-pop .sb-check{display:flex;align-items:center;gap:8px;color:var(--ui-text-primary)}
.sb-check input{margin:0;accent-color:var(--ui-accent)}
.sb-pchip{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 7px 0 6px;border-radius:5px;font-size:11.5px;font-weight:500;line-height:1;white-space:nowrap;background:var(--ui-bg-tertiary);color:var(--ui-text-secondary)}
.sb-pchip .codicon{font-size:12px}
.sb-pchip-ok{color:color-mix(in srgb,var(--ui-green) 62%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-green) 13%,transparent)}
.sb-pchip-warn{color:color-mix(in srgb,var(--ui-yellow) 48%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-yellow) 16%,transparent)}
.sb-pchip-err{color:color-mix(in srgb,var(--ui-red) 66%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-red) 13%,transparent)}
.sb-pchip-acc{color:color-mix(in srgb,var(--ui-accent) 66%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-accent) 12%,transparent)}
button.sb-pchip{border:0;cursor:pointer;font-family:inherit}
.sb-chips{display:flex;gap:4px;flex-wrap:wrap;margin-top:7px}
.sb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:22px 18px}
.sb-tile{position:relative;border-radius:12px;outline:none;cursor:pointer;min-width:0}
.sb-thumb-wrap{position:relative}
.sb-thumb{position:relative;aspect-ratio:16/9;border-radius:10px;overflow:hidden;background:var(--ui-bg-tertiary);transition:transform .2s cubic-bezier(.2,.8,.2,1)}
.sb-thumb img,.sb-tile-live{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.sb-tile:hover .sb-thumb{transform:translateY(-2px)}
.sb-tile:focus-visible{outline:none}
.sb-tile:focus-visible .sb-thumb{box-shadow:0 0 0 2px var(--ui-accent),0 0 0 4px var(--ui-chat-surface-background)}
.sb-tile[data-open=true] .sb-thumb{box-shadow:0 0 0 2px var(--ui-accent)}
.sb-nothumb{position:absolute;inset:0;display:grid;place-items:center;background:repeating-linear-gradient(135deg,var(--ui-bg-tertiary) 0 8px,transparent 8px 16px)}
.sb-nothumb span{padding:3px 8px;border-radius:6px;font-size:12px;background:var(--ui-bg-elevated);color:var(--ui-text-secondary)}
.sb-sel{position:absolute;left:8px;top:8px;z-index:2;width:24px;height:24px;display:grid;place-items:center;border-radius:6px;background:rgb(12 13 16/.62);opacity:0;transition:opacity .15s}
.sb-sel input{margin:0;width:15px;height:15px;accent-color:var(--ui-accent);cursor:pointer}
.sb-tile:hover .sb-sel,.sb-tile:focus .sb-sel,.sb-selecting .sb-sel,.sb-sel:has(input:checked){opacity:1}
.sb-tname{display:flex;align-items:center;gap:8px;margin-top:9px;min-width:0}
.sb-tname b{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-tsub{font-size:12px;color:var(--ui-text-secondary);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-list{display:grid;gap:2px}
.sb-lrow{display:grid;grid-template-columns:24px 112px minmax(0,1fr) minmax(0,auto);gap:14px;align-items:center;padding:8px;border-radius:10px;cursor:pointer;outline:none}
.sb-lrow:hover{background:var(--ui-row-hover-background)}
.sb-lrow:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.sb-lrow[data-open=true]{background:var(--ui-row-active-background)}
.sb-lrow .sb-thumb{border-radius:6px}
.sb-lrow .sb-sel{position:static;opacity:1;background:none}
.sb-lrow .sb-chips{margin:0;justify-content:flex-end}
.sb-skel .sb-thumb{animation:sb-pulse 1.4s ease-in-out infinite}
.sb-skel-line{height:10px;border-radius:5px;background:var(--ui-bg-tertiary);margin-top:10px;width:70%}
.sb-skel-short{width:40%}
.sb-skel-stage{animation:sb-pulse 1.4s ease-in-out infinite;background:var(--ui-bg-tertiary)}
@keyframes sb-pulse{50%{opacity:.55}}
.sb-pickup{display:grid;grid-template-columns:112px minmax(0,1fr) auto;gap:16px;align-items:center;padding:8px 12px 8px 8px;border-radius:12px;border:1px solid var(--ui-stroke-tertiary);margin:12px 0 18px}
.sb-pickup .sb-thumb{border-radius:7px}
.sb-pickup small{display:block;font-size:12px;color:var(--ui-text-secondary)}
.sb-pickup b{font-size:14px}
.sb-pick-actions{display:flex;gap:6px;flex-wrap:wrap}
.sb-banner{display:flex;gap:10px;align-items:center;padding:7px 8px 7px 12px;border-radius:10px;background:var(--ui-bg-tertiary);margin-bottom:8px;font-size:12.5px}
.sb-banner>.codicon{flex:none;color:var(--ui-text-secondary)}
.sb-banner>div{min-width:0}
.sb-banner p{display:inline;margin:0;color:var(--ui-text-secondary)}
.sb-banner b{font-weight:600;margin-right:6px}
.sb-banner ul+*,.sb-banner p+ul{display:block}
.sb-banner ul{margin:6px 0 0;padding-left:18px;color:var(--ui-text-secondary)}
.sb-banner code{font:11.5px var(--dt-font-mono);color:var(--ui-text-primary)}
.sb-banner-actions{display:flex;gap:6px;margin-left:auto;flex:none;align-self:center}
.sb-jobline{color:var(--ui-text-secondary);font-variant-numeric:tabular-nums}
.sb-more{display:flex;justify-content:center;margin-top:22px}
.sb-bulk{position:sticky;bottom:12px;z-index:10;display:flex;flex-direction:column;align-items:center;gap:8px;margin-top:16px;pointer-events:none}
.sb-bulkdel{pointer-events:auto;width:min(520px,100%);background-color:var(--ui-bg-elevated);box-shadow:0 8px 24px rgb(0 0 0/.2)}
.sb-selall{align-self:center;margin:0;width:15px;height:15px;accent-color:var(--ui-accent);cursor:pointer}
.sb-bulkbar{pointer-events:auto;display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;border-radius:10px;background:var(--ui-bg-elevated);border:1px solid var(--ui-stroke-secondary);box-shadow:0 8px 24px rgb(0 0 0/.2)}
.sb-empty{display:grid;justify-items:center;text-align:center;padding:56px 20px}
.sb-empty>.codicon{font-size:28px;color:var(--ui-text-secondary)}
.sb-empty h2{font-size:18px;margin:10px 0 6px;font-weight:600}
.sb-empty p{margin:0 auto 14px;max-width:48ch;color:var(--ui-text-secondary)}
.sb-prompt{text-align:left;font:12.5px/1.5 var(--dt-font-mono);padding:10px 12px;border-radius:8px;background:var(--ui-bg-tertiary);max-width:520px;margin:0 auto 14px;white-space:pre-wrap}
.sb-dim{position:absolute;inset:0;z-index:20;background:color-mix(in srgb,var(--ui-chat-surface-background) 55%,transparent)}
.sb-sheet{position:absolute;top:0;right:0;bottom:0;z-index:21;width:min(560px,calc(100% - 40px));overflow:auto;background:var(--ui-bg-elevated);border-left:1px solid var(--ui-stroke-secondary);box-shadow:-18px 0 40px rgb(0 0 0/.22);animation:sb-sheet-in .22s cubic-bezier(.2,.8,.2,1)}
@keyframes sb-sheet-in{from{transform:translateX(24px);opacity:.4}}
.sb-shead{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:8px;padding:12px 14px 10px 18px;background:var(--ui-bg-elevated)}
.sb-shead h2{margin:0;font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-sbody{padding:0 18px 28px}
.sb-pstage{position:relative;aspect-ratio:16/9;border-radius:10px;overflow:hidden;background:#0b0b0d}
.sb-pstage img,.sb-pcanvas{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:cover}
.sb-pcanvas{cursor:default;outline:none}
.sb-gate{display:flex;align-items:center;gap:10px;margin:8px 0 12px;padding:8px 10px;border-radius:9px;background:var(--ui-bg-tertiary);font-size:12.5px}
.sb-gate p{margin:0;flex:1;color:var(--ui-text-secondary)}
.sb-player{display:flex;align-items:center;gap:8px;margin:6px 0 12px}
.sb-player input[type=range]{flex:1;margin:0;accent-color:var(--ui-accent);cursor:ew-resize}
.sb-ptime{font:12px/1 var(--dt-font-mono);font-variant-numeric:tabular-nums;color:var(--ui-text-secondary);min-width:4.6em}
.sb-problem-flat{margin:8px 0}
.sb-sactions{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.sb-sactions2{display:flex;flex-wrap:wrap;gap:2px;margin:0 -8px 12px}
.sb-toaster{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:40;pointer-events:none}
.sb-toast-box{pointer-events:auto;display:flex;align-items:center;gap:8px;width:max-content;max-width:min(640px,calc(100vw - 64px));padding:6px 6px 6px 12px;border-radius:10px;background:var(--ui-bg-elevated);border:1px solid var(--ui-stroke-secondary);box-shadow:0 8px 24px rgb(0 0 0/.22);font-size:12.5px}
.sb-toast-box .sb-btn{height:24px;padding:0 8px;font-size:12px}
@media (prefers-reduced-motion:reduce){.sb-shell *,.sb-lab *{transition:none!important;animation:none!important}}
`

const DETAIL_CSS = `
.sb-sec{border-top:1px solid var(--ui-stroke-tertiary);padding:12px 0 8px}
.sb-sec .sb-row{grid-template-columns:minmax(6rem,10rem) 1fr 3.4rem}
.sb-kv{display:grid;grid-template-columns:96px minmax(0,1fr);gap:8px 10px;font-size:12.5px;margin:4px 0 12px}
.sb-kv dt{color:var(--ui-text-secondary);line-height:22px}
.sb-kv dd{margin:0;display:flex;gap:6px;align-items:center;flex-wrap:wrap;min-height:22px;min-width:0}
.sb-star-on{color:color-mix(in srgb,var(--ui-yellow) 70%,var(--ui-text-primary))}
.sb-note{margin:0 0 6px;font-size:12px;color:var(--ui-text-secondary);max-width:62ch}
.sb-inline-err{margin:6px 0 0;font-size:12px;color:color-mix(in srgb,var(--ui-red) 66%,var(--ui-text-primary))}
.sb-inline-err ul{margin:4px 0 0;padding-left:18px}
.sb-lic-list{flex-basis:100%;display:grid;gap:2px;margin-top:6px;padding:8px;border-radius:9px;border:1px solid var(--ui-stroke-tertiary)}
.sb-lic-opt{display:grid;grid-template-columns:18px 1fr;gap:8px;align-items:start;padding:6px;border-radius:7px;cursor:pointer}
.sb-lic-opt:hover{background:var(--ui-row-hover-background)}
.sb-lic-opt input{margin:3px 0 0;accent-color:var(--ui-accent)}
.sb-lic-opt b{font-weight:600}
.sb-lic-opt small{display:block;font-size:12px;color:var(--ui-text-secondary)}
.sb-lic-opt[aria-disabled=true]{cursor:not-allowed}
.sb-lic-opt[aria-disabled=true] b{color:var(--ui-text-secondary)}
.sb-reason{color:color-mix(in srgb,var(--ui-yellow) 48%,var(--ui-text-primary))!important}
.sb-chip-x{border:0;background:none;padding:0;margin-left:2px;color:inherit;cursor:pointer;display:inline-grid;place-items:center;border-radius:4px}
.sb-chip-x:hover{color:var(--ui-text-primary)}
.sb-tag-input{height:22px;width:110px;font-size:12px;padding:0 6px;border-radius:6px}
.sb-inline-form{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 12px}
.sb-inline-form .sb-field{flex:1;min-width:180px}
.sb-danger{display:grid;gap:8px;margin:0 0 12px;padding:10px 12px;border-radius:10px;border:1px solid color-mix(in srgb,var(--ui-red) 40%,transparent);background:color-mix(in srgb,var(--ui-red) 6%,transparent);font-size:12.5px}
.sb-danger p{margin:0;color:var(--ui-text-secondary)}
.sb-danger-btn{color:color-mix(in srgb,var(--ui-red) 70%,var(--ui-text-primary));border-color:color-mix(in srgb,var(--ui-red) 45%,transparent)}
.sb-film{display:flex;gap:10px;overflow-x:auto;list-style:none;margin:0;padding:2px 2px 8px;scroll-snap-type:x proximity}
.sb-frame{flex:none;width:132px;display:grid;gap:4px;align-content:start;scroll-snap-align:start}
.sb-frame-open{display:grid;gap:3px;padding:4px;margin:-4px;border:0;border-radius:9px;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}
.sb-frame-open:hover{background:var(--ui-row-hover-background)}
.sb-frame[aria-current=true] .sb-fthumb{box-shadow:0 0 0 2px var(--ui-accent)}
.sb-fthumb{display:block;aspect-ratio:16/9;border-radius:6px;overflow:hidden;background:var(--ui-bg-tertiary)}
.sb-fthumb img{width:100%;height:100%;object-fit:cover;display:block}
.sb-fnone{display:block;width:100%;height:100%;background:repeating-linear-gradient(135deg,var(--ui-bg-tertiary) 0 6px,transparent 6px 12px)}
.sb-frame-line{display:flex;gap:6px;align-items:baseline;font-size:12px}
.sb-frame-line span,.sb-frame-date{color:var(--ui-text-secondary);font-size:11.5px}
.sb-frame-tick{display:flex;gap:6px;align-items:center;font-size:11.5px;color:var(--ui-text-secondary);cursor:pointer}
.sb-frame-tick input{margin:0;accent-color:var(--ui-accent)}
.sb-compare{border:1px solid var(--ui-stroke-tertiary);border-radius:10px;padding:10px;margin:4px 0 10px}
.sb-cmp-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:12.5px}
.sb-cmp-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sb-cmp-cell{margin:0}
.sb-cmp-cell .sb-thumb{border-radius:7px}
.sb-cmp-cell figcaption{font-size:12px;color:var(--ui-text-secondary);margin-top:4px}
.sb-cmp-sliders{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:4px;font-size:12px}
.sb-cmp-sliders li{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sb-cmp-sliders code,.sb-files code{font:11.5px var(--dt-font-mono)}
.sb-diff{margin-top:10px;max-height:300px;overflow:auto;border-radius:8px;background:var(--ui-bg-tertiary);font:11.5px/1.55 var(--dt-font-mono)}
.sb-dline{display:grid;grid-template-columns:2.6em 2.6em 1.2em 1fr;white-space:pre}
.sb-dno{text-align:right;padding-right:6px;color:var(--ui-text-secondary);user-select:none}
.sb-dop{color:var(--ui-text-secondary);user-select:none}
.sb-dline[data-op=add]{background:color-mix(in srgb,var(--ui-green) 16%,transparent)}
.sb-dline[data-op=del]{background:color-mix(in srgb,var(--ui-red) 14%,transparent)}
.sb-dfold{padding:2px 10px;color:var(--ui-text-secondary);font-family:var(--dt-font-sans);font-size:11.5px;border-block:1px dashed var(--ui-stroke-secondary)}
.sb-tree{list-style:none;margin:0;padding:0;font-size:12.5px}
.sb-tree li{position:relative;padding:3px 0 3px 16px}
.sb-tree li::before{content:"";position:absolute;left:4px;top:0;bottom:0;border-left:1px solid var(--ui-stroke-secondary)}
.sb-tree li::after{content:"";position:absolute;left:4px;top:13px;width:8px;border-top:1px solid var(--ui-stroke-secondary)}
.sb-tree .sb-tree{margin-top:2px}
.sb-treelink{border:0;background:none;padding:0;color:var(--ui-text-primary);font:inherit;cursor:pointer;text-decoration:underline;text-decoration-color:var(--ui-stroke-secondary);text-underline-offset:3px}
.sb-treelink:hover{text-decoration-color:currentColor}
.sb-form{display:grid;gap:10px}
.sb-form-row{display:grid;gap:4px;font-size:12px;color:var(--ui-text-secondary)}
.sb-form-row .sb-field{color:var(--ui-text-primary)}
.sb-files{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:4px}
.sb-files li{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 4px 4px 8px;border-radius:8px;background:var(--ui-bg-tertiary);font-size:12px}
`

const LAB_CSS = `
.sb-lab{position:relative;height:100%;min-height:0;container-type:inline-size;font-size:13px;line-height:1.45;color:var(--ui-text-primary)}
.sb-lab *{box-sizing:border-box}
.sb-lab :focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.sb-lab-frame{height:100%;display:flex;flex-direction:column;min-height:0}
.sb-lab-head{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--ui-stroke-tertiary);min-width:0}
.sb-lab-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.sb-kbd{margin-left:2px;font-size:11px;font-weight:400;letter-spacing:.02em}
.sb-lab-status{font-size:12px;color:var(--ui-text-secondary);white-space:nowrap}
.sb-lab-note{margin:6px 12px 0;font-size:12px;color:var(--ui-text-secondary)}
.sb-lab-banner{margin:8px 12px 0}
.sb-lab-body{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,40%)}
.sb-lab-code{display:flex;flex-direction:column;min-height:0;min-width:0;border-right:1px solid var(--ui-stroke-tertiary)}
.sb-lab-side{min-height:0;overflow:auto;padding:12px}
@container (max-width:760px){.sb-lab-body{display:flex;flex-direction:column;overflow:auto}.sb-lab-side{order:-1;flex:none;overflow:visible}.sb-lab-code{flex:none;height:560px;border-right:0;border-top:1px solid var(--ui-stroke-tertiary)}}
.sb-ed{flex:1;min-height:240px;display:grid;grid-template-columns:3.6em minmax(0,1fr);font:12px/20px var(--dt-font-mono);overflow:hidden}
.sb-ed-gutter{overflow:hidden;padding:8px 0;text-align:right;color:var(--ui-text-secondary);user-select:none;background:var(--ui-bg-tertiary)}
.sb-ed-gutter div{height:20px;padding-right:8px;font-variant-numeric:tabular-nums}
.sb-ed-gutter div[data-mark=error]{color:color-mix(in srgb,var(--ui-red) 70%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-red) 18%,transparent);font-weight:600}
.sb-ed-gutter div[data-mark=warn]{color:color-mix(in srgb,var(--ui-yellow) 50%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-yellow) 16%,transparent)}
.sb-ed-main{position:relative;min-width:0;min-height:0;overflow:hidden}
.sb-ed-back{position:absolute;inset:0;pointer-events:none}
.sb-ed-bar{position:absolute;left:0;right:-4000px;height:20px}
.sb-ed-bar[data-mark=error]{background:color-mix(in srgb,var(--ui-red) 12%,transparent)}
.sb-ed-bar[data-mark=warn]{background:color-mix(in srgb,var(--ui-yellow) 12%,transparent)}
.sb-ed-text{position:relative;display:block;width:100%;height:100%;margin:0;padding:8px 10px;border:0;outline:none;resize:none;background:transparent;color:var(--ui-text-primary);caret-color:var(--ui-accent);font:inherit;white-space:pre;overflow:auto;tab-size:2}
.sb-ed-text:focus-visible{outline:none;box-shadow:inset 0 0 0 2px var(--ui-accent)}
.sb-lab-markers{list-style:none;margin:0;padding:6px;max-height:132px;overflow:auto;border-top:1px solid var(--ui-stroke-tertiary);font-size:12px}
.sb-lab-markers li>.sb-count{display:block;padding:4px 6px}
.sb-lab-markers button{display:flex;gap:6px;align-items:baseline;width:100%;padding:4px 6px;border:0;border-radius:6px;background:none;color:var(--ui-text-primary);font:inherit;text-align:left;cursor:pointer}
.sb-lab-markers button:hover{background:var(--ui-row-hover-background)}
.sb-lab-markers b{white-space:nowrap}
.sb-notes{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.sb-notes li{display:grid;gap:2px}
.sb-notes p{margin:0;font-size:12.5px}
.sb-notes .sb-link{justify-self:start;margin:0;padding:0;font-size:11.5px}
.sb-lab-tab{display:flex;flex-direction:column;min-height:100%}
.sb-lab-tabbody{flex:1;min-height:560px;height:calc(100vh - 170px);border:1px solid var(--ui-stroke-tertiary);border-radius:10px;overflow:hidden}
.sb-lab-choose{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 10px}
.sb-btn-on{border-color:var(--ui-accent)}
`

export default {
  id: ID,
  name: 'Shaderbrücke',
  register(ctx) {
    pluginApi = ctx
    pluginStore = ctx.storage && typeof ctx.storage.get === 'function' && typeof ctx.storage.set === 'function' ? ctx.storage : null
    pluginFiles = ctx.files && typeof ctx.files.requestFolder === 'function' && typeof ctx.files.writeText === 'function' ? ctx.files : null
    mountStyles()

    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(unmountStyles)
    }

    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA === 'string') {
      ctx.register({
        id: 'shader-canvas',
        area: sdk.TOOL_MEDIA_RENDERER_AREA,
        data: {
          tools: ['shader_render', 'shader_library'],
          render: props => (drawsCard(props.toolName, props.args) ? jsx(ShaderCanvas, props) : props.fallback)
        }
      })
    }

    // The page needs a route area and the app's React Query client; a Desktop without them keeps the card only.
    if (typeof sdk.ROUTES_AREA !== 'string' || typeof sdk.useQuery !== 'function' || typeof sdk.useQueryClient !== 'function') {
      return
    }

    ctx.register({ id: 'page', area: sdk.ROUTES_AREA, title: 'Shaderbrücke', data: { path: PAGE_PATH }, render: () => jsx(ShadersPage, {}) })

    if (typeof sdk.SIDEBAR_NAV_AREA === 'string') {
      ctx.register({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, data: { codicon: 'symbol-color', label: 'Shaderbrücke', path: PAGE_PATH } })
    }

    if (typeof sdk.PALETTE_AREA === 'string') {
      ctx.register({ id: 'open', area: sdk.PALETTE_AREA, data: { id: 'shaderbruecke.open', label: 'Open Shaderbrücke', keywords: ['shader', 'shaderbruecke', 'library', 'gallery', 'glsl'], run: () => goPage({}) } })
      ctx.register({ id: 'new', area: sdk.PALETTE_AREA, data: { id: 'shaderbruecke.new', label: 'New shader', keywords: ['shader', 'create', 'glsl'], run: () => newShader() } })
    }
  }
}
