// The library's thumbnail requests, run in node against desktop/plugin.js itself.
//
// usage: node tests/test_desktop_thumbs.mjs   (prints one JSON line per case, exits 1 on a failure)
//
// The three imports the Desktop half may use are replaced by small stand-ins, and the module's
// `loadThumbs` and `thumbItem` are exposed with a recording `rest`. It proves which keys each
// GET /thumbs carries and in which order; the timing it buys is measured in experiments/.
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
export const __probe = { loadThumbs, thumbItem, setApi: api => { pluginApi = api } }
`

async function load() {
  const text = fs.readFileSync(PLUGIN_JS, 'utf8')
  const imports = /^import .* from '(?:@hermes\/plugin-sdk|react|react\/jsx-runtime)'\n/gm
  if ((text.match(imports) || []).length !== 3) {
    throw new Error('desktop/plugin.js no longer has exactly its three allowed imports')
  }
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'sb-thumbs-'))
  const file = path.join(dir, 'plugin.mjs')
  fs.writeFileSync(file, STUBS + text.replace(imports, '') + PROBE)
  try {
    return await import(pathToFileURL(file).href)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

const results = []
async function check(name, fn) {
  try {
    const detail = await fn()
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

const { __probe: probe } = await load()

// A page of 48 rows of which the first 6 tiles are on screen, like the Shaders page at 1280 px.
async function requests(count, onScreen, stampOffset) {
  const calls = []
  probe.setApi({
    rest: path => {
      const keys = decodeURIComponent(path.replace(/^\/thumbs\?ids=/, '')).split(',')
      calls.push(keys)
      return Promise.resolve({ thumbs: Object.fromEntries(keys.map(key => [key, `data:image/webp;base64,${key}`])) })
    }
  })
  const items = Array.from({ length: count }, (_, i) => probe.thumbItem(`shader-${i}`, 1 + (i % 3), stampOffset + i))
  await probe.loadThumbs(items, onScreen ? item => onScreen(Number(item.id.split('-')[1])) : undefined)
  return { calls, items }
}

await check('the tiles on screen come in the first request, alone', async () => {
  const { calls, items } = await requests(48, i => i < 6, 1000)
  equal(calls[0], items.slice(0, 6).map(item => item.key), 'first /thumbs keys')
  equal(calls.length, 2, 'request count')
  equal(calls[1], items.slice(6).map(item => item.key), 'second /thumbs keys')
  return { requests: calls.map(keys => keys.length) }
})

await check('tiles on screen further down the page still come first, in page order', async () => {
  const { calls, items } = await requests(20, i => i >= 9 && i < 12, 2000)
  equal(calls[0], items.slice(9, 12).map(item => item.key), 'first /thumbs keys')
  equal(calls[1], [...items.slice(0, 9), ...items.slice(12)].map(item => item.key), 'second /thumbs keys')
})

await check('without an on-screen rule, batches of 60 in page order as before', async () => {
  const { calls, items } = await requests(130, null, 3000)
  equal(calls.map(keys => keys.length), [60, 60, 10], 'batch sizes')
  equal(calls.flat(), items.map(item => item.key), 'keys in page order')
})

await check('no tile on screen yet: one request for the page, not an empty first one', async () => {
  const { calls } = await requests(48, () => false, 4000)
  equal(calls.map(keys => keys.length), [48], 'batch sizes')
})

for (const line of results) console.log(JSON.stringify(line))
const failed = results.filter(r => !r.ok).length
process.exit(failed ? 1 : 0)
