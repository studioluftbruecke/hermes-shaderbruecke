// The settings page and the sidebar row, run in node against desktop/plugin.js itself.
//
// usage: node tests/test_desktop_settings.mjs   (prints one JSON line per case, exits 1 on a failure)
//
// The three imports (@hermes/plugin-sdk, react, react/jsx-runtime) are replaced by stand-ins, as in
// test_desktop_card.mjs. `register(ctx)` runs against a fake ctx whose `register` replaces an entry
// by id and whose disposers remove by id, like Desktop's registry. It proves what the plugin
// registers and renders; Desktop's own Settings and sidebar code are proven in
// experiments/sidebar-rows/ (M0 probe), and the look in the page harness.
import fs from 'fs'
import os from 'os'
import path from 'path'
import { fileURLToPath, pathToFileURL } from 'url'

const here = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(here, '..')
const PLUGIN_JS = path.join(ROOT, 'desktop', 'plugin.js')

const STUBS = `
const tinyAtom = v => {
  const ls = new Set()
  return { get: () => v, set: n => { v = n; ls.forEach(l => l(v)) }, listen: l => (ls.add(l), () => ls.delete(l)) }
}
const navigations = []
const sdk = {
  TOOL_MEDIA_RENDERER_AREA: 'transcript.toolMedia', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav', PALETTE_AREA: 'palette',
  useQuery: () => ({}), useQueryClient: () => ({}), Switch: 'Switch', Input: 'Input', atom: tinyAtom, useValue: a => a.get(),
  host: { navigate: p => navigations.push(p), state: { connectionId: tinyAtom('local') } }
}
const noop = () => {}
const memo = c => c, useCallback = f => f, useEffect = noop, useId = () => 'id', useMemo = f => f()
const useReducer = (r, s) => [s, noop], useRef = v => ({ current: v }), useState = v => [v, noop]
const jsx = (type, props, key) => ({ type, props, key }), jsxs = jsx
`
const PROBE = `
export const __probe = { sdk, navigations, SidebarSection, SettingsPage, RemoteNote, plugin: typeof __default === 'undefined' ? null : __default }
`

async function load() {
  const text = fs.readFileSync(PLUGIN_JS, 'utf8')
  const imports = /^import .* from '(?:@hermes\/plugin-sdk|react|react\/jsx-runtime)'\n/gm
  if ((text.match(imports) || []).length !== 3) {
    throw new Error('desktop/plugin.js no longer has exactly its three allowed imports')
  }
  const body = text.replace(imports, '').replace(/^export default \{/m, 'const __default = {')
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'sb-settings-'))
  const file = path.join(dir, 'plugin.mjs')
  fs.writeFileSync(file, STUBS + body + PROBE)
  try {
    return (await import(pathToFileURL(file).href)).__probe
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

function fakeCtx(stored) {
  const rows = []
  const pages = []
  const disposers = []
  const store = new Map(stored === undefined ? [] : [['sidebar', JSON.stringify(stored)]])
  return {
    rows, pages, disposers,
    storage: { get: (k, f) => (store.has(k) ? JSON.parse(store.get(k)) : f), set: (k, v) => store.set(k, JSON.stringify(v)) },
    stored: () => (store.has('sidebar') ? JSON.parse(store.get('sidebar')) : undefined),
    register(c) {
      const i = rows.findIndex(r => r.id === c.id)
      if (i >= 0) rows.splice(i, 1)
      rows.push(c)
      return () => { const j = rows.findIndex(r => r.id === c.id); if (j >= 0) rows.splice(j, 1) }
    },
    registerSettingsPage(page) {
      pages.push(page)
      return () => {}
    },
    onDispose: fn => disposers.push(fn)
  }
}

// Expand function components (no hooks beyond sdk.useValue here) into a tree of host elements.
function expand(node) {
  if (node == null || typeof node !== 'object') return node
  if (Array.isArray(node)) return node.map(expand)
  if (typeof node.type === 'function') return expand(node.type(node.props || {}))
  const children = node.props && node.props.children
  return { ...node, props: { ...node.props, children: expand(children) } }
}
const texts = n => (n == null || typeof n !== 'object' ? (typeof n === 'string' ? [n] : []) : Array.isArray(n) ? n.flatMap(texts) : texts(n.props && n.props.children))
const find = (n, test) => (n == null || typeof n !== 'object' ? [] : Array.isArray(n) ? n.flatMap(c => find(c, test)) : [...(test(n) ? [n] : []), ...find(n.props && n.props.children, test)])
const navRows = ctx => ctx.rows.filter(r => r.area === 'sidebar.nav')

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

const probe = await load()
const { plugin } = probe
const label = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard', 'manifest.json'), 'utf8')).label
const slug = /^name:\s*(\S+)/m.exec(fs.readFileSync(path.join(ROOT, 'plugin.yaml'), 'utf8'))[1]

check('one settings page, titled with the brand (D7: no entry under the slug)', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  equal(ctx.pages.map(p => [p.id, p.title, p.icon, typeof p.render]), [['settings', label, 'symbol-color', 'function']], 'pages')
  equal([plugin.id, plugin.name], [slug, label], 'Desktop id and name')
  return { title: ctx.pages[0].title, id: plugin.id }
})

check('the sidebar row: one row, the brand at 52, the Shaders page', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  equal(navRows(ctx).map(r => [r.id, r.order, r.data.label, r.data.path, r.data.codicon]), [['nav', 52, 'Shaderbrücke', '/shaderbruecke', 'symbol-color']], 'nav')
})

check('the hint names a palette row that exists: Open Shaderbrücke', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  const labels = ctx.rows.filter(r => r.area === 'palette').map(r => r.data.label)
  if (!labels.includes('Open Shaderbrücke')) throw new Error(`palette: ${labels}`)
})

check('a stored choice: hidden stays hidden, a stored position is used', () => {
  let ctx = fakeCtx({ show: false, position: 52 })
  plugin.register(ctx)
  equal(navRows(ctx).length, 0, 'hidden rows')
  ctx = fakeCtx({ show: true, position: 7 })
  plugin.register(ctx)
  equal(navRows(ctx).map(r => r.order), [7], 'order')
})

check('the settings page: Sidebar section, then Agent settings', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  const tree = expand(ctx.pages[0].render())
  const all = texts(tree)
  equal(all, ['Sidebar', 'Show in the sidebar', 'Position', 'Lower numbers sit higher. Kanban is 50.',
    'Agent settings', 'Your name and link in export headers, the credit line, kept versions, project writes and the starter prompt. They apply to the next call.',
    'Open Agent settings'], 'texts')
  if (all.some(s => /[\u2013\u2014]/.test(s))) throw new Error('a dash in the copy')
})

check('switching the row off from the page removes it, keeps one entry, and shows the palette hint', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  let tree = expand(ctx.pages[0].render())
  find(tree, n => n.type === 'Switch')[0].props.onCheckedChange(false)
  equal(navRows(ctx).length, 0, 'rows after off')
  equal(ctx.stored(), { show: false, position: 52 }, 'stored')
  tree = expand(ctx.pages[0].render())
  if (!texts(tree).includes('Open it from the command palette: Open Shaderbrücke.')) throw new Error(texts(tree).join('|'))
  find(tree, n => n.type === 'Switch')[0].props.onCheckedChange(true)
  const field = find(expand(ctx.pages[0].render()), n => n.type === 'Input')[0]
  field.props.onKeyDown({ key: 'Enter', target: { value: '30' } })
  equal(navRows(ctx).map(r => [r.order, r.data.label]), [[30, 'Shaderbrücke']], 'one row after on and a move')
})

check('Open Agent settings goes to the schema form folded beneath this page', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  probe.navigations.length = 0
  const button = find(expand(ctx.pages[0].render()), n => n.type === 'button' && texts(n).includes('Open Agent settings'))[0]
  button.props.onClick()
  // contrib/settings-pages.ts pluginSettingsRouteHref({ agent: 'shaderbruecke' }) builds this route;
  // the agent key of a plugin installed at plugins/shaderbruecke is its name.
  equal(probe.navigations, [`/settings?tab=plugins&agent=${slug}`], 'navigate')
  return { href: probe.navigations[0] }
})

check('the two-place line shows only on a remote connection', () => {
  const state = probe.sdk.host.state.connectionId
  state.set('local')
  equal(expand(probe.RemoteNote({})), null, 'local')
  state.set('remote-1')
  equal(texts(expand(probe.RemoteNote({}))), ['Hermes on another computer? Update this plugin on both. Here: ', `hermes plugins update ${slug}`, ', then Rescan.'], 'remote')
  state.set('local')
})

check('unload disposes the row and the page renders without the section', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  ctx.disposers.forEach(fn => fn())
  equal(navRows(ctx).length, 0, 'rows after unload')
  equal(texts(expand(ctx.pages[0].render()))[0], 'Agent settings', 'first text')
})

for (const r of results) console.log(JSON.stringify(r))
process.exit(results.every(r => r.ok) ? 0 : 1)
