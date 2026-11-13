// "Load more" against a delete: a page merged while the refetch after a mutation runs is replayed
// by that refetch, so the list ends with every loaded page and no skipped row. Runs the plugin's
// real useLibrary query function and mergePage against a fake offset-cursor server (48 a page).
// The Load more click itself lives in the page component; its merge rule is restated here:
// merge only when the cache still carries the cursor the button showed.
//
// usage: node tests/test_desktop_library_pages.mjs   (one JSON line per case, exits 1 on a failure)
import { check, equal, finish, loadPlugin } from './desktop_harness.mjs'

const cache = new Map()
let options = null
globalThis.__sbSdk = {
  TOOL_MEDIA_RENDERER_AREA: 'transcript.toolMedia',
  useQueryClient: () => ({ getQueryData: k => cache.get(JSON.stringify(k)) }),
  useQuery: given => { options = given; return {} }
}
const p = await loadPlugin(['useLibrary', 'mergePage'])

const PAGE = 48
let server = []
const gates = []
/** The fake server: answers from its rows at send time, once the test releases the request. */
async function serve(route) {
  const url = new URL(`http://x${route}`)
  const offset = Number(url.searchParams.get('cursor') || 0)
  const snapshot = server.slice()
  await new Promise(resolve => gates.push(resolve))
  return { rows: snapshot.slice(offset, offset + PAGE), total: snapshot.length,
    cursor: offset + PAGE < snapshot.length ? String(offset + PAGE) : null, jobs: [] }
}
p.setApi({ rest: serve })
const flush = () => new Promise(resolve => setTimeout(resolve, 0))
const release = async (index = 0) => { await flush(); gates.splice(index, 1)[0](); await flush() }

p.useLibrary('')
const key = JSON.stringify(options.queryKey)
const refetch = () => options.queryFn({ queryKey: options.queryKey })
const ids = data => data.rows.map(row => Number(row.id.slice(1)))

async function loadMore(cursor) {
  const next = await serve(`/library?cursor=${cursor}`)
  const current = cache.get(key)
  if (current && current.cursor === cursor) cache.set(key, p.mergePage(current, next))
}

await check('a Load more that lands during the refetch after a delete is kept, with no skipped row', async () => {
  server = Array.from({ length: 100 }, (_, i) => ({ id: `s${i}` }))
  cache.clear()
  const first = refetch(); await release(); cache.set(key, await first)
  const cursor = cache.get(key).cursor
  server = server.filter(row => row.id !== 's3')
  const running = refetch(); await flush()
  const more = loadMore(cursor); await flush()
  await release(1); await more
  await release(0)
  while (gates.length) await release(0)
  cache.set(key, await running)
  const data = cache.get(key)
  equal(data.pages, 2, 'pages after the refetch')
  equal(ids(data), server.slice(0, 96).map(row => Number(row.id.slice(1))), 'rows after the refetch')
})

await check('a refetch with nothing merged meanwhile replays the pages it started with', async () => {
  server = Array.from({ length: 100 }, (_, i) => ({ id: `s${i}` }))
  cache.clear()
  const first = refetch(); await release(); cache.set(key, await first)
  const more = loadMore(cache.get(key).cursor); await release(); await more
  server = server.filter(row => row.id !== 's7')
  const running = refetch()
  while (true) { await flush(); if (!gates.length) break; await release(0) }
  const data = await running
  equal([data.pages, ids(data).length], [2, 96], 'two pages')
})

finish()
