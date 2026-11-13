// "Share files" never replaces a file in the folder the user picked, and its toast counts the
// files that were actually written. Run in node against desktop/plugin.js.
//
// usage: node tests/test_desktop_share.mjs   (prints one JSON line per case, exits 1 on a failure)
//
// The server names each file after the shader's slug, and the slug folds case and punctuation and
// maps a name with no a-z0-9 to "shader", so a batch can hold the same name twice. The fake folder
// below honours `exclusive` the way Desktop's file bridge does (error `exists`).
import { check, equal, finish, loadPlugin } from './desktop_harness.mjs'

const p = await loadPlugin(['saveShareFiles', 'toastSubscribers'])

function folder(existing = {}) {
  const disk = new Map(Object.entries(existing))
  const calls = []
  p.setFiles({
    requestFolder: async () => ({ ok: true, folder: { id: 'f1', name: 'Shares' } }),
    writeText: async (id, file, text, options) => {
      calls.push({ file, exclusive: Boolean(options && options.exclusive) })
      if (options && options.exclusive && disk.has(file)) return { ok: false, error: 'exists', message: 'A file with that name already exists.' }
      disk.set(file, text)
      return { ok: true, path: file }
    }
  })
  return { disk, calls }
}

function serve(filenames) {
  p.setApi({ rest: async route => {
    const i = Number(decodeURIComponent(route).match(/\/shaders\/s(\d+)/)[1])
    return { filename: filenames[i], share: { format: 'shaderbruecke/1', name: `shader ${i}`, source: `// ${i}` } }
  } })
}

const toasts = []
p.toastSubscribers.add(toast => toasts.push(toast.text))

await check('same-name shaders in one batch and a file already there are all kept', async () => {
  const { disk, calls } = folder({ 'sky.shaderbruecke.json': 'mine\n' })
  serve(['sky.shaderbruecke.json', 'sky.shaderbruecke.json', 'shader.shaderbruecke.json', 'shader.shaderbruecke.json'])
  const answer = await p.saveShareFiles([0, 1, 2, 3].map(i => ({ id: `s${i}` })))
  equal(disk.get('sky.shaderbruecke.json'), 'mine\n', 'the file the user kept')
  equal(disk.size, 5, 'files in the folder')
  equal(answer.written, ['sky-2.shaderbruecke.json', 'sky-3.shaderbruecke.json', 'shader.shaderbruecke.json',
    'shader-2.shaderbruecke.json'], 'names written')
  equal(calls.every(call => call.exclusive), true, 'every write is exclusive')
  equal(toasts.at(-1), 'Saved 4 share files to Shares.', 'toast')
})

await check('the toast counts only files that landed', async () => {
  const { disk } = folder()
  serve(['a.shaderbruecke.json', 'b.shaderbruecke.json'])
  p.setFiles({
    requestFolder: async () => ({ ok: true, folder: { id: 'f1', name: 'Shares' } }),
    writeText: async (id, file, text) => (file.startsWith('b') ? { ok: false, error: 'io', message: 'disk full' } : (disk.set(file, text), { ok: true, path: file }))
  })
  const answer = await p.saveShareFiles([{ id: 's0' }, { id: 's1' }])
  equal(answer.written, ['a.shaderbruecke.json'], 'written')
  equal(toasts.at(-1), 'Saved 1 of 2 share files to Shares.', 'toast')
})

await check('one shader keeps its plain name when the folder has room', async () => {
  const { disk } = folder()
  serve(['glow.shaderbruecke.json'])
  const answer = await p.saveShareFiles([{ id: 's0' }])
  equal([answer.written, [...disk.keys()]], [['glow.shaderbruecke.json'], ['glow.shaderbruecke.json']], 'single')
  equal(toasts.at(-1), 'Saved glow.shaderbruecke.json to Shares.', 'toast')
})

finish()
