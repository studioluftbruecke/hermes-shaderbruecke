/**
 * shaderbruecke desktop half: draws the `.frag` files the `shader_render` tool produced as a
 * live WebGL2 canvas inside the chat transcript (TOOL_MEDIA_RENDERER_AREA), with the shader's
 * sliders under it, a local lint before every compile, and export/copy actions that go through
 * the plugin API (`/api/plugins/shaderbruecke/...`).
 *
 * The shader already compiled in the headless preflight on the Hermes host, so only checked code
 * reaches this GPU. The header and footer below must match HEADER/FOOTER in renderer.py.
 */
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
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

// Mirrors record.py: `// @slider <name> <min>..<max> = <default> [step <s>]`. The Python test
// tests/test_slider_header.py holds both implementations to the same samples.
const SLIDER_RE = /^\s*\/\/\s*@slider\s+([A-Za-z_]\w*)\s+(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)(?:\s+step\s+(-?\d+(?:\.\d+)?))?\s*$/
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
    hint: 'write `void mainImage(out vec4 fragColor, in vec2 fragCoord)` only' }
]

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

/** The findings for a source: same rules, same order as checks.lint. */
function lintSource(source) {
  const stripped = stripComments(source)
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

/** Parse `@slider` annotations into {name, min, max, default, step}, like record.parse_sliders. */
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

    sliders.push({ name, min, max, default: value, step: match[5] ? Number(match[5]) : null })
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

  return gl.getShaderParameter(shader, gl.COMPILE_STATUS)
    ? [shader, '']
    : [null, (gl.getShaderInfoLog(shader) || 'compile failed').replace(/\0/g, '').trim()]
}

const IDLE_CONTROL = { toggle() {}, stop() {}, setSlider() {} }

/** Compile `source`, then draw it every animation frame until `stop()` is called. */
function startRenderer(canvas, source, sliders, onState) {
  const errors = lintSource(source).filter(finding => finding.severity === 'error')

  if (errors.length) {
    onState({ status: 'lint', findings: errors })

    return IDLE_CONTROL
  }

  const gl = canvas.getContext('webgl2', { antialias: false, premultipliedAlpha: false })

  if (!gl) {
    onState({ status: 'nowebgl' })

    return IDLE_CONTROL
  }

  const [vs, vsLog] = compile(gl, gl.VERTEX_SHADER, VERTEX)
  const [fs, fsLog] = compile(gl, gl.FRAGMENT_SHADER, headerFor(sliders || []) + source + FOOTER)

  if (!vs || !fs) {
    onState({ status: 'error', log: vsLog || fsLog })

    return IDLE_CONTROL
  }

  const prog = gl.createProgram()
  gl.attachShader(prog, vs)
  gl.attachShader(prog, fs)
  gl.bindAttribLocation(prog, 0, 'p')
  gl.linkProgram(prog)

  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    onState({ status: 'error', log: gl.getProgramInfoLog(prog) || 'link failed' })

    return IDLE_CONTROL
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
      gl.uniform1f(location, slider.default)
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

  const mouse = [0, 0, 0, 0]
  let raf = 0
  let last = 0
  let elapsed = 0
  let slow = 0
  let running = true
  let visible = true
  let reduced = false

  try {
    reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    reduced = false
  }

  const drawOnce = seconds => {
    const d = new Date()
    gl.viewport(0, 0, canvas.width, canvas.height)
    gl.uniform3f(loc.res, canvas.width, canvas.height, 1)
    gl.uniform1f(loc.time, seconds)
    gl.uniform1f(loc.delta, 1 / 60)
    gl.uniform1i(loc.frame, Math.round(seconds * 60))
    gl.uniform4f(loc.mouse, mouse[0], mouse[1], mouse[2], mouse[3])
    gl.uniform4f(loc.date, d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds())
    gl.drawArrays(gl.TRIANGLES, 0, 3)
  }

  const onMove = event => {
    const rect = canvas.getBoundingClientRect()
    mouse[0] = (event.clientX - rect.left) * (canvas.width / rect.width)
    mouse[1] = (rect.bottom - event.clientY) * (canvas.height / rect.height)
  }

  canvas.addEventListener('pointermove', onMove)

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
      onState({ status: 'paused', reason: `frames took over ${SLOW_FRAME_MS} ms` })

      return
    }

    drawOnce(elapsed)
    raf = requestAnimationFrame(draw)
  }

  const resize = new ResizeObserver(() => {
    const ratio = Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO)
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * ratio))
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * ratio))
  })

  resize.observe(canvas)

  const seen = new IntersectionObserver(entries => {
    visible = entries.some(entry => entry.isIntersecting)
    last = 0

    if (visible && running && !raf) {
      raf = requestAnimationFrame(draw)
    }
  })

  seen.observe(canvas)

  if (reduced) {
    drawOnce(1)
    onState({ status: 'still', reason: 'prefers-reduced-motion' })
  } else {
    onState({ status: 'live' })
    raf = requestAnimationFrame(draw)
  }

  return {
    toggle() {
      running = !running
      last = 0
      slow = 0

      if (running && !raf) {
        raf = requestAnimationFrame(draw)
      }

      onState({ status: running ? 'live' : 'paused', reason: running ? '' : 'paused' })
    },
    setSlider(name, value) {
      const location = sliderUniforms[name]

      if (location) {
        gl.uniform1f(location, value)
      }
    },
    stop() {
      running = false
      cancelAnimationFrame(raf)
      resize.disconnect()
      seen.disconnect()
      canvas.removeEventListener('pointermove', onMove)
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  }
}

/** One API handle for the pane; `register(ctx)` sets it. */
let pluginApi = null

function apiCall(path, options) {
  if (!pluginApi || typeof pluginApi.rest !== 'function') {
    return Promise.reject(new Error('the plugin API is not available'))
  }

  return pluginApi.rest(path, options)
}

const BUTTON = {
  fontSize: '0.72rem',
  padding: '0.15rem 0.5rem',
  borderRadius: '6px',
  border: '1px solid var(--ui-stroke-secondary)',
  background: 'transparent',
  color: 'var(--ui-text-secondary)',
  cursor: 'pointer'
}

function SliderRow({ sliders, values, onChange, onCommit }) {
  if (!sliders.length) {
    return null
  }

  const muted = { color: 'var(--ui-text-tertiary)' }

  return jsx('div', {
    style: { display: 'grid', gap: '0.2rem', marginTop: '0.35rem' },
    children: sliders.map(slider => jsxs('label', {
      key: slider.name,
      style: { display: 'grid', gridTemplateColumns: '9rem 1fr 3.5rem', alignItems: 'center', gap: '0.5rem', fontSize: '0.72rem', ...muted },
      children: [
        jsx('span', { children: slider.name }),
        jsx('input', {
          type: 'range',
          min: slider.min,
          max: slider.max,
          step: slider.step || 0.01,
          value: values[slider.name],
          onChange: event => onChange(slider.name, Number(event.target.value)),
          onPointerUp: onCommit,
          style: { width: '100%' }
        }),
        jsx('span', { style: { textAlign: 'right', fontVariantNumeric: 'tabular-nums' }, children: Number(values[slider.name]).toFixed(2) })
      ]
    }))
  })
}

function Toolbar({ targets, onCopy, onCompose, status }) {
  const [target, setTarget] = useState('three')
  const rows = targets.length ? targets : [{ id: 'three', label: 'three.js' }]

  return jsxs('div', {
    style: { display: 'flex', gap: '0.35rem', alignItems: 'center', marginTop: '0.35rem', flexWrap: 'wrap' },
    children: [
      jsx('select', {
        value: target,
        onChange: event => setTarget(event.target.value),
        style: { ...BUTTON, paddingRight: '0.25rem' },
        children: rows.map(row => jsx('option', { value: row.id, children: row.label }, row.id))
      }),
      jsx('button', { style: BUTTON, onClick: () => onCopy(target), children: 'Copy file' }),
      jsx('button', { style: BUTTON, onClick: onCompose, children: 'Copy for agent' }),
      status ? jsx('span', { style: { fontSize: '0.7rem', color: 'var(--ui-text-tertiary)' }, children: status }) : null
    ]
  })
}

function ShaderCanvas({ file, args, result, fallback, sessionId }) {
  const canvasRef = useRef(null)
  const controlRef = useRef(null)
  const [source, setSource] = useState(null)
  const [state, setState] = useState({ status: 'loading' })
  const [status, setStatus] = useState('')
  const [targets, setTargets] = useState([])
  const meta = parseResult(result)
  const title = (args && typeof args.name === 'string' && args.name) || meta.name || file.name
  const sliders = useMemo(
    () => (Array.isArray(meta.sliders) && meta.sliders.length
      ? meta.sliders
      : parseSliders((args && args.source) || '')),
    [meta.sliders, args]
  )
  const [values, setValues] = useState(() => {
    const initial = {}

    for (const slider of sliders) {
      initial[slider.name] = slider.default
    }

    return initial
  })

  useEffect(() => {
    let live = true
    const fromArgs = args && typeof args.source === 'string' ? args.source : null

    file
      .resolveUrl()
      .then(url => (typeof url === 'string' ? decodeDataUrl(url) : null))
      .catch(() => null)
      .then(text => {
        if (!live) {
          return
        }

        const src = text || fromArgs

        if (src) {
          setSource(src)
        } else {
          setState({ status: 'unavailable' })
        }
      })

    return () => {
      live = false
    }
  }, [file, args])

  useEffect(() => {
    if (!meta.id) {
      return
    }

    let live = true

    apiCall(`/shaders/${meta.id}/targets`)
      .then(payload => {
        if (live && payload && Array.isArray(payload.targets)) {
          setTargets(payload.targets)
        }
      })
      .catch(() => {})

    return () => {
      live = false
    }
  }, [meta.id])

  useEffect(() => {
    if (!source || !canvasRef.current) {
      return
    }

    const control = startRenderer(canvasRef.current, source, sliders, setState)
    controlRef.current = control

    return () => {
      controlRef.current = null
      control.stop?.()
    }
  }, [source, sliders])

  const onSlider = (name, value) => {
    setValues(current => ({ ...current, [name]: value }))
    controlRef.current?.setSlider?.(name, value)
  }

  const commitSliders = () => {
    if (!meta.id) {
      return
    }

    apiCall(`/shaders/${meta.id}/slider_values`, { method: 'POST', body: { values } })
      .then(() => setStatus('sliders saved'))
      .catch(() => setStatus('sliders not saved (no plugin API)'))
  }

  const copyExport = async target => {
    try {
      const payload = await apiCall(`/shaders/${meta.id}/export/${target}`)
      await sdk.writeClipboardText(payload.text)
      setStatus(`${payload.main_file} copied`)
    } catch (error) {
      setStatus(`not exported yet (${String(error && error.message ? error.message : error)})`)
    }
  }

  const copyForAgent = async () => {
    try {
      const payload = await apiCall(`/shaders/${meta.id}/export/agent`)
      await sdk.writeClipboardText(payload.text)
      setStatus('handoff copied')
    } catch (error) {
      setStatus(`handoff unavailable (${String(error && error.message ? error.message : error)})`)
    }
  }

  const putInComposer = async () => {
    try {
      const payload = await apiCall(`/shaders/${meta.id}/export/agent`)
      const inserted = await sdk.host.composer.insertText(sessionId || null, payload.text, { mode: 'block' })

      setStatus(inserted ? 'handoff put in the composer' : 'composer insert refused')
    } catch {
      await sdk.writeClipboardText(`Export ${title} as an agent handoff`)
      setStatus('copied a request instead; paste it in the composer')
    }
  }

  if (state.status === 'unavailable' || state.status === 'nowebgl') {
    return fallback
  }

  const muted = { color: 'var(--ui-text-tertiary)' }
  const label =
    state.status === 'live'
      ? 'live · WebGL2 · click to pause'
      : state.status === 'still'
        ? 'still frame (reduced motion) · click to play'
        : state.status === 'paused'
          ? `paused (${state.reason}) · click to play`
          : state.status === 'lint'
            ? 'lint found a problem before compiling'
            : state.status === 'error'
              ? 'compile error on this GPU'
              : 'loading…'

  const lintLines = state.status === 'lint'
    ? (state.findings || []).map(finding => `line ${finding.line}: ${finding.message} — ${finding.hint}`).join('\n')
    : null

  return jsxs('figure', {
    'data-shaderbruecke': state.status,
    style: { margin: '0.5rem 0', maxWidth: '640px' },
    children: [
      jsx('canvas', {
        ref: canvasRef,
        onClick: () => controlRef.current?.toggle?.(),
        style: {
          width: '100%',
          aspectRatio: '16 / 9',
          display: 'block',
          borderRadius: '8px',
          border: '1px solid var(--ui-stroke-secondary)',
          cursor: 'pointer'
        }
      }),
      state.status === 'error' || state.status === 'lint'
        ? jsx('pre', {
            style: { ...muted, whiteSpace: 'pre-wrap', fontSize: '0.75rem', margin: '0.25rem 0 0' },
            children: lintLines || state.log
          })
        : null,
      jsx(SliderRow, { sliders, values, onChange: onSlider, onCommit: commitSliders }),
      jsxs('figcaption', {
        style: { ...muted, fontSize: '0.75rem', marginTop: '0.25rem', display: 'flex', gap: '0.5rem' },
        children: [
          jsx('span', { style: { color: 'var(--ui-text-secondary)' }, children: title }),
          jsx('span', { children: label })
        ]
      }),
      meta.id
        ? jsx(Toolbar, { targets, onCopy: copyExport, onCompose: copyForAgent, status })
        : null,
      meta.id
        ? jsx('button', {
            style: { ...BUTTON, marginTop: '0.3rem' },
            onClick: putInComposer,
            children: 'Put handoff in composer'
          })
        : null
    ]
  })
}

export default {
  id: ID,
  name: 'Shaderbrücke',
  register(ctx) {
    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA !== 'string') {
      return
    }

    pluginApi = ctx

    ctx.register({
      id: 'shader-canvas',
      area: sdk.TOOL_MEDIA_RENDERER_AREA,
      data: {
        tools: ['shader_render'],
        render: props => jsx(ShaderCanvas, props)
      }
    })
  }
}
