// Shared harness for the Desktop half's node tests: load desktop/plugin.js with its three imports
// replaced by stand-ins and named module-private functions exposed, a fake WebGL2 canvas that
// counts what the player asks of the GPU, and a small hooks runner that renders one component
// with state, refs and effects. It proves the plugin's own logic, not Desktop's renderer.
import fs from 'fs'
import os from 'os'
import path from 'path'
import { fileURLToPath, pathToFileURL } from 'url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const PLUGIN_JS = path.resolve(here, '..', 'desktop', 'plugin.js')

const STUBS = `
const sdk = globalThis.__sbSdk || { TOOL_MEDIA_RENDERER_AREA: 'transcript.toolMedia' }
const noop = () => {}
const __h = () => globalThis.__sbHooks || {}
const memo = c => c, useId = () => 'id'
const useCallback = (f, d) => (__h().useCallback ? __h().useCallback(f, d) : f)
const useEffect = (f, d) => (__h().useEffect ? __h().useEffect(f, d) : undefined)
const useMemo = (f, d) => (__h().useMemo ? __h().useMemo(f, d) : f())
const useReducer = (r, s) => (__h().useReducer ? __h().useReducer(r, s) : [s, noop])
const useRef = v => (__h().useRef ? __h().useRef(v) : { current: v })
const useState = v => (__h().useState ? __h().useState(v) : [typeof v === 'function' ? v() : v, noop])
const jsx = (type, props, key) => ({ type, props, key }), jsxs = jsx
`

/** The module with `names` exposed as `__sb`, plus setters for the module's API and file handles. */
export async function loadPlugin(names) {
  const text = fs.readFileSync(PLUGIN_JS, 'utf8')
  const imports = /^import .* from '(?:@hermes\/plugin-sdk|react|react\/jsx-runtime)'\n/gm
  if ((text.match(imports) || []).length !== 3) {
    throw new Error('desktop/plugin.js no longer has exactly its three allowed imports')
  }
  const exposed = names.map(name => `${name}: typeof ${name} === 'undefined' ? null : ${name}`).join(', ')
  const probe = `\nexport const __sb = { ${exposed}, setApi: api => { pluginApi = api }, setFiles: f => { pluginFiles = f } }\n`
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'sb-desktop-'))
  const file = path.join(dir, 'plugin.mjs')
  fs.writeFileSync(file, STUBS + text.replace(imports, '') + probe)
  try {
    return (await import(pathToFileURL(file).href)).__sb
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

// ---- results ----------------------------------------------------------------------------------

const results = []

/** Run one case; `fn` throws (or rejects) to fail it. Prints one JSON line per case. */
export async function check(name, fn) {
  try {
    const detail = await fn()
    results.push({ name, ok: true, ...(detail ? { detail } : {}) })
  } catch (error) {
    results.push({ name, ok: false, error: String(error && error.message ? error.message : error) })
  }
}

export function equal(actual, expected, what) {
  const a = JSON.stringify(actual)
  const b = JSON.stringify(expected)
  if (a !== b) throw new Error(`${what}: ${a} !== ${b}`)
}

export function finish() {
  for (const line of results) console.log(JSON.stringify(line))
  process.exitCode = results.every(line => line.ok) && results.length ? 0 : 1
}

// ---- browser globals and a fake WebGL2 canvas ---------------------------------------------------

export const gpu = { getContext: 0, loseContext: 0, deleteShader: 0, deleteProgram: 0, compile: 0, draws: 0 }
export const frames = { queue: [], next: 0 }
export const motion = { reduced: false }

export function installBrowser() {
  class WebGLProgram {}
  globalThis.WebGLProgram = WebGLProgram
  globalThis.requestAnimationFrame = fn => { frames.next += 1; frames.queue.push({ id: frames.next, fn }); return frames.next }
  globalThis.cancelAnimationFrame = id => { frames.queue = frames.queue.filter(item => item.id !== id) }
  globalThis.window = {
    matchMedia: () => ({ matches: motion.reduced }), devicePixelRatio: 1, setTimeout, clearTimeout,
    CSS: { escape: s => s }
  }
  globalThis.ResizeObserver = class { observe() {} disconnect() {} }
  globalThis.IntersectionObserver = class { observe() {} disconnect() {} }
  globalThis.performance = globalThis.performance || { now: () => Date.now() }
}

export function resetGpu() {
  for (const key of Object.keys(gpu)) gpu[key] = 0
  frames.queue = []
  motion.reduced = false
}

function fakeGl({ failCompile = false, failLink = false, lostAtStart = false } = {}) {
  let lost = lostAtStart
  return {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4, ARRAY_BUFFER: 5,
    STATIC_DRAW: 6, FLOAT: 7, TRIANGLES: 8,
    isContextLost: () => lost,
    createShader: () => ({}),
    shaderSource() {},
    compileShader() { gpu.compile += 1 },
    getShaderParameter: () => !(failCompile && gpu.compile % 2 === 0),
    getShaderInfoLog: () => 'ERROR: 0:1: fake',
    deleteShader() { gpu.deleteShader += 1 },
    createProgram: () => new globalThis.WebGLProgram(),
    attachShader() {}, bindAttribLocation() {}, linkProgram() {},
    getProgramParameter: () => !failLink,
    getProgramInfoLog: () => 'link fake',
    deleteProgram() { gpu.deleteProgram += 1 },
    useProgram() {}, bindBuffer() {}, createBuffer: () => ({}), bufferData() {},
    enableVertexAttribArray() {}, vertexAttribPointer() {},
    getUniformLocation: (program, name) => ({ name }),
    uniform1f() {}, uniform2f() {}, uniform3f() {}, uniform4f() {}, uniform1i() {}, viewport() {},
    drawArrays() { gpu.draws += 1 },
    getExtension: name => (name === 'WEBGL_lose_context' ? { loseContext() { gpu.loseContext += 1; lost = true } } : null)
  }
}

export function fakeCanvas(glOptions) {
  const listeners = {}
  let gl = null
  return {
    width: 300, height: 150, clientWidth: 300, clientHeight: 150,
    getContext() { gpu.getContext += 1; gl = gl || fakeGl(glOptions); return gl },
    addEventListener(type, fn) { (listeners[type] ||= new Set()).add(fn) },
    removeEventListener(type, fn) { if (listeners[type]) listeners[type].delete(fn) },
    dispatch(type) { for (const fn of listeners[type] || []) fn({ type, preventDefault() {} }) },
    listenerCount: () => Object.values(listeners).reduce((n, set) => n + set.size, 0),
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 150, width: 300, height: 150 })
  }
}

// ---- a minimal hooks runner ---------------------------------------------------------------------

const changed = (a, b) => !a || !b || a.length !== b.length || a.some((d, k) => !Object.is(d, b[k]))

/** Render `Component(props)` until it settles: state slots, refs attached to its canvas, effects. */
export async function renderComponent(Component, props, canvas) {
  const slots = []
  let index = 0
  let pending = []
  let dirty = false
  const hooks = {
    useState(init) {
      const i = index++
      if (!(i in slots)) slots[i] = typeof init === 'function' ? init() : init
      return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; dirty = true }]
    },
    useReducer(reducer, init) {
      const i = index++
      if (!(i in slots)) slots[i] = init
      return [slots[i], action => { const next = reducer(slots[i], action); if (next !== slots[i]) { slots[i] = next; dirty = true } }]
    },
    useRef(v) { const i = index++; if (!(i in slots)) slots[i] = { current: v }; return slots[i] },
    useMemo(fn, deps) {
      const i = index++
      if (!(i in slots) || changed(slots[i].deps, deps)) slots[i] = { deps, value: fn() }
      return slots[i].value
    },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps) },
    useEffect(fn, deps) {
      const i = index++
      const prev = slots[i]
      if (!prev || !deps || changed(prev.deps, deps)) {
        slots[i] = { deps, cleanup: prev && prev.cleanup }
        pending.push(() => { if (slots[i].cleanup) slots[i].cleanup(); slots[i].cleanup = fn() })
      }
    }
  }
  const attach = node => {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(attach)
    if (node.type === 'canvas' && node.props && node.props.ref) node.props.ref.current = canvas
    if (node.props) attach(node.props.children)
  }
  const words = []
  const stateWord = node => {
    if (!node || typeof node !== 'object') return null
    if (Array.isArray(node)) { for (const n of node) { const w = stateWord(n); if (w) return w } return null }
    if (node.props && node.props.className === 'sb-state') {
      return [].concat(node.props.children).find(k => typeof k === 'string') || null
    }
    return node.props ? stateWord(node.props.children) : null
  }
  globalThis.__sbHooks = hooks
  try {
    for (let round = 0; round < 12; round += 1) {
      index = 0; pending = []; dirty = false
      const tree = Component(props)
      attach(tree)
      const word = stateWord(tree)
      if (word) words.push(word)
      pending.forEach(run => run())
      await new Promise(resolve => setTimeout(resolve, 5))
      if (!dirty && !pending.length) break
    }
  } finally {
    globalThis.__sbHooks = null
  }
  return words
}
