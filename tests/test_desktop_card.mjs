// The chat card's gate and result parsing, run in node against desktop/plugin.js itself.
//
// usage: node tests/test_desktop_card.mjs   (prints one JSON line per case, exits 1 on a failure)
//
// The three imports the Desktop half may use (@hermes/plugin-sdk, react, react/jsx-runtime) are
// replaced by small stand-ins, and the module's own `cardMeta`, `drawsCard` and `sliderValuesPath`
// are exposed for the test. It proves the plugin's logic, not Desktop's renderer or the canvas,
// nor which tool call Desktop picks as the card's producer.
import fs from 'fs'
import os from 'os'
import path from 'path'
import { fileURLToPath, pathToFileURL } from 'url'

const here = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_JS = path.resolve(here, '..', 'desktop', 'plugin.js')

const STUBS = `
const sdk = { TOOL_MEDIA_RENDERER_AREA: 'transcript.toolMedia' }
const noop = () => {}
const memo = c => c, useCallback = f => f, useEffect = noop, useId = () => 'id', useMemo = f => f()
const useReducer = (r, s) => [s, noop], useRef = v => ({ current: v }), useState = v => [v, noop]
const jsx = (type, props) => ({ type, props }), jsxs = jsx
`
const PROBE = `
export const __probe = {
  cardMeta: typeof cardMeta === 'function' ? cardMeta : null,
  drawsCard: typeof drawsCard === 'function' ? drawsCard : null,
  sliderValuesPath: typeof sliderValuesPath === 'function' ? sliderValuesPath : null,
  ShaderCanvas
}
`

async function load() {
  const text = fs.readFileSync(PLUGIN_JS, 'utf8')
  const imports = /^import .* from '(?:@hermes\/plugin-sdk|react|react\/jsx-runtime)'\n/gm
  if ((text.match(imports) || []).length !== 3) {
    throw new Error('desktop/plugin.js no longer has exactly its three allowed imports')
  }
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'sb-card-'))
  const file = path.join(dir, 'plugin.mjs')
  fs.writeFileSync(file, STUBS + text.replace(imports, '') + PROBE)
  try {
    return await import(pathToFileURL(file).href)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

const SOURCE = '// @slider glow 0.0..2.0 = 0.8 step 0.01 "Glow"\n// @slider speed 0.0..3.0 = 1.0\nvoid mainImage(out vec4 c, in vec2 p) { c = vec4(glow * speed); }\n'
const SLIDERS = [
  { name: 'glow', min: 0, max: 2, default: 0.8, step: 0.01, label: 'Glow' },
  { name: 'speed', min: 0, max: 3, default: 1, step: null }
]
const FRAG = '/data/shaders/ember/v3/shader.frag'
const PLAY = {
  success: true, id: 'ember', version: 3, name: 'Ember ridges', frag_path: FRAG, sliders: SLIDERS,
  tuned_values: { glow: 1.5 }, checks: { status: 'ok' }, media: `MEDIA:${FRAG}`
}
const IMPORT = {
  success: true, id: 'pasted', version: 1, name: 'Pasted glow', dialect: 'isf', licence: 'MIT', warnings: [],
  sliders: SLIDERS, tuned_values: {}, media: 'MEDIA:/data/shaders/pasted/v1/shader.frag'
}
const RENDER = { success: true, id: 'ember', version: 4, name: 'ember ridges', sliders: SLIDERS, tuned_values: {} }

const results = []
function check(name, fn) {
  try {
    const detail = fn()
    results.push({ name, ok: true, ...(detail ? { detail } : {}) })
  } catch (error) {
    results.push({ name, ok: false, error: String(error && error.message ? error.message : error) })
  }
}
function equal(actual, expected, what) {
  const a = JSON.stringify(actual)
  const b = JSON.stringify(expected)
  if (a !== b) throw new Error(`${what}: ${a} !== ${b}`)
}

const mod = await load()
const { cardMeta, drawsCard, sliderValuesPath, ShaderCanvas } = mod.__probe

const contributions = []
mod.default.register({ register: c => contributions.push(c) })
const card = contributions.find(c => c.id === 'shader-canvas')

check('the card is registered for shader_render and shader_library', () => {
  equal(card && card.data.tools, ['shader_render', 'shader_library'], 'tools')
})

const FALLBACK = { fallback: true }
const render = (toolName, args, result) => card.data.render({
  file: { path: FRAG, name: 'shader.frag' }, toolName, toolCallId: 't1', args, result, sessionId: null, fallback: FALLBACK
})

check('play and import draw the card; get, list, share and fork keep the plain file', () => {
  equal(render('shader_library', { action: 'play', id: 'ember' }, PLAY).type === ShaderCanvas, true, 'play')
  equal(render('shader_library', { action: 'import', source: SOURCE }, IMPORT).type === ShaderCanvas, true, 'import')
  equal(render('shader_render', { source: SOURCE }, RENDER).type === ShaderCanvas, true, 'render')
  for (const action of ['get', 'list', 'share', 'fork', undefined]) {
    equal(render('shader_library', action ? { action, id: 'ember' } : {}, { success: true }), FALLBACK, String(action))
  }
})

check('drawsCard reads the action the way the handler does', () => {
  equal(drawsCard('shader_library', { action: 'PLAY' }), true, 'upper case')
  equal(drawsCard('shader_library', null), false, 'no args')
  equal(drawsCard('shader_render', {}), true, 'render')
})

check('a play result gives the card its id, version, name, sliders and tuned values', () => {
  for (const result of [PLAY, JSON.stringify(PLAY)]) {
    equal(cardMeta('shader_library', { action: 'play', id: 'ember', version: 3 }, result),
      { id: 'ember', version: 3, name: 'Ember ridges', sliders: SLIDERS, tunedValues: { glow: 1.5 }, argsSource: '', autoplay: false },
      typeof result)
  }
})

check('an import result never plays the pasted original, only the converted frag', () => {
  for (const result of [IMPORT, JSON.stringify(IMPORT)]) {
    equal(cardMeta('shader_library', { action: 'import', source: '/*{ "ISF": 1 }*/ void main(){}', name: 'x' }, result),
      { id: 'pasted', version: 1, name: 'Pasted glow', sliders: SLIDERS, tunedValues: {}, argsSource: '', autoplay: false },
      typeof result)
  }
})

check('a render result keeps the source and name from its arguments', () => {
  // A render result without sliders falls back to the `@slider` lines of its own source.
  const parsed = [
    { name: 'glow', min: 0, max: 2, default: 0.8, step: 0.01, label: 'Glow' },
    { name: 'speed', min: 0, max: 3, default: 1, step: null, label: null }
  ]
  equal(cardMeta('shader_render', { source: SOURCE, name: 'Ember' }, { ...RENDER, sliders: [] }),
    { id: 'ember', version: 4, name: 'Ember', sliders: parsed, tunedValues: {}, argsSource: SOURCE, autoplay: false },
    'render')
})

check('slider values save on the version the card shows', () => {
  equal(sliderValuesPath('ember', 3), '/shaders/ember/slider_values?version=3', 'versioned')
  equal(sliderValuesPath('ember', null), '/shaders/ember/slider_values', 'latest')
})

for (const line of results) {
  console.log(JSON.stringify(line))
}
const failed = results.filter(r => !r.ok).length
console.log(JSON.stringify({ passed: results.length - failed, failed }))
process.exit(failed ? 1 : 0)
