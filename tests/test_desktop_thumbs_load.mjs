// The library's thumbnail requests: batches go out together (at most three at a time, the tiles on
// screen first), and a batch that failed is asked for again later instead of reading "No preview
// yet" for the rest of the Desktop session. Run in node against desktop/plugin.js.
//
// usage: node tests/test_desktop_thumbs_load.mjs   (one JSON line per case, exits 1 on a failure)
import { check, equal, finish, loadPlugin } from './desktop_harness.mjs'

const p = await loadPlugin(['loadThumbs', 'thumbItem', 'thumbCache', 'thumbValue', 'thumbWanted', 'THUMB_RETRY_MS'])
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const RTT = 40

const items = (count, prefix) => Array.from({ length: count }, (_, i) => p.thumbItem(`${prefix}-${i}`, 1, 1000 + i))

function api({ fail = () => false } = {}) {
  const calls = []
  let open = 0
  p.setApi({
    rest: async route => {
      const keys = decodeURIComponent(route.replace(/^\/thumbs\?ids=/, '')).split(',')
      open += 1
      calls.push({ keys, start: Date.now(), open })
      const number = calls.length
      await sleep(RTT)
      open -= 1
      if (fail(number)) throw new Error('502: Bad Gateway')
      return { thumbs: Object.fromEntries(keys.map(key => [key, `data:image/webp;base64,${key}`])) }
    }
  })
  return calls
}

await check('the off-screen batch does not wait for the on-screen one', async () => {
  const calls = api()
  const list = items(48, 'page')
  const started = Date.now()
  await p.loadThumbs(list, item => Number(item.id.split('-')[1]) < 6)
  equal(calls.map(call => call.keys.length), [6, 42], 'batches in order, on-screen first')
  equal(calls[1].start - calls[0].start < RTT / 2, true, `second batch started ${calls[1].start - calls[0].start} ms later`)
  equal(Date.now() - started < 2 * RTT, true, 'one round trip for the page')
})

await check('at most three batches are in flight at once', async () => {
  const calls = api()
  await p.loadThumbs(items(500, 'many'), null)
  equal(calls.length, 9, 'batches of at most 60')
  equal(Math.max(...calls.map(call => call.open)), 3, 'in flight')
})

await check('a failed batch shows no preview now and is asked for again after the retry window', async () => {
  api({ fail: n => n === 1 })
  const list = items(10, 'fail')
  await p.loadThumbs(list, item => Number(item.id.split('-')[1]) < 4)
  const failed = list.slice(0, 4)
  equal(failed.map(item => p.thumbValue(item.entry)), [null, null, null, null], 'shown as no preview')
  equal(failed.some(item => p.thumbCache.has(item.entry)), false, 'not cached for the session')
  equal(failed.map(item => p.thumbWanted(item.entry)), [false, false, false, false], 'not asked again at once')
  const later = Date.now() + p.THUMB_RETRY_MS
  equal(failed.map(item => p.thumbWanted(item.entry, later)), [true, true, true, true], 'asked again later')
  equal(list.slice(4).every(item => typeof p.thumbValue(item.entry) === 'string'), true, 'the other batch landed')
  api()
  await p.loadThumbs(failed, null)
  equal(failed.every(item => typeof p.thumbValue(item.entry) === 'string'), true, 'the retry fills them in')
})

finish()
