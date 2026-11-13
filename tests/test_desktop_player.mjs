// The WebGL player and the chat card's autoplay rule, run in node against desktop/plugin.js.
//
// usage: node tests/test_desktop_player.mjs   (prints one JSON line per case, exits 1 on a failure)
//
// A fake WebGL2 context counts contexts, compiles, deletions and draws, so the cases see what the
// player asks of the GPU: a lint error takes no context, every failure path gives its context
// back, stop() releases everything, a lost context is reported as lost, reduced motion and
// autoplay:false draw one still frame. The card cases render ShaderCanvas with a small hooks
// runner and check that only a version whose flash verdict is `none` starts playing by itself.
import {
  check, equal, fakeCanvas, finish, frames, gpu, installBrowser, loadPlugin, motion, renderComponent, resetGpu
} from './desktop_harness.mjs'

installBrowser()
const p = await loadPlugin(['createShaderPlayer', 'ShaderCanvas', 'playGate', 'cardMeta', 'SliderRow'])
p.setApi({ rest: async () => ({ targets: [], tuned_values: {} }) })

const GOOD = 'void mainImage(out vec4 c, in vec2 p) { c = vec4(p.x / iResolution.x, 0.0, 0.0, 1.0); }'
const HANG = 'void mainImage(out vec4 c, in vec2 p) { while (true) { } c = vec4(1.0); }'

await check('a lint error never asks for a WebGL context', () => {
  resetGpu()
  const phases = []
  p.createShaderPlayer(fakeCanvas(), { source: HANG, onState: s => phases.push(s.phase) })
  equal([gpu.getContext, phases[0]], [0, 'lint'], 'context and phase')
})

await check('20 failed compiles each give their own context back', () => {
  resetGpu()
  const phases = []
  for (let i = 0; i < 20; i += 1) {
    p.createShaderPlayer(fakeCanvas({ failCompile: true }), { source: GOOD, onState: s => phases.push(s.phase) })
  }
  equal([gpu.loseContext, [...new Set(phases)]], [20, ['error']], 'lost contexts and phases')
})

await check('a link failure deletes the program and gives the context back', () => {
  resetGpu()
  const phases = []
  p.createShaderPlayer(fakeCanvas({ failLink: true }), { source: GOOD, onState: s => phases.push(s.phase) })
  equal([gpu.loseContext, gpu.deleteProgram, phases[0]], [1, 1, 'error'], 'link failure')
})

await check('stop() gives the context back, removes its listeners and leaves no frame queued', () => {
  resetGpu()
  const canvas = fakeCanvas()
  const control = p.createShaderPlayer(canvas, { source: GOOD })
  const before = canvas.listenerCount()
  control.stop()
  equal([before > 0, canvas.listenerCount(), gpu.loseContext, frames.queue.length], [true, 0, 1, 0], 'after stop')
})

await check('a canvas whose context is already lost reports lost, not a compile error', () => {
  resetGpu()
  const phases = []
  p.createShaderPlayer(fakeCanvas({ lostAtStart: true }), { source: GOOD, onState: s => phases.push(s.phase) })
  equal([phases[0], gpu.compile], ['lost', 0], 'lost at start')
})

await check('webglcontextlost stops the loop; play() afterwards does nothing', () => {
  resetGpu()
  const phases = []
  const canvas = fakeCanvas()
  const control = p.createShaderPlayer(canvas, { source: GOOD, onState: s => phases.push(s.phase) })
  canvas.dispatch('webglcontextlost')
  control.play()
  equal([phases.includes('lost'), phases[phases.length - 1], frames.queue.length], [true, 'lost', 0], 'lost')
})

await check('reduced motion: one still frame at t = 1 s, no loop until play()', () => {
  resetGpu()
  motion.reduced = true
  const phases = []
  const control = p.createShaderPlayer(fakeCanvas(), { source: GOOD, onState: s => phases.push(s.phase) })
  const still = [phases[0], gpu.draws, frames.queue.length, control.time()]
  control.play()
  equal([...still, frames.queue.length], ['still', 1, 0, 1, 1], 'still then one frame')
})

await check('autoplay: false holds a still frame like reduced motion', () => {
  resetGpu()
  const phases = []
  p.createShaderPlayer(fakeCanvas(), { source: GOOD, autoplay: false, onState: s => phases.push(s.phase) })
  equal([phases[0], frames.queue.length], ['still', 0], 'held')
})

// ---- the chat card ------------------------------------------------------------------------------

const dataUrl = `data:text/plain;base64,${Buffer.from(GOOD).toString('base64')}`
const renderResult = flash => JSON.stringify({
  success: true, id: 'strobe', version: 1, name: 'strobe', sliders: [], tuned_values: {},
  checks: flash === undefined ? { status: 'ok' } : { status: 'ok', flat: false, flash },
  media: 'MEDIA:/data/shader.frag'
})

async function card(result, toolName = 'shader_render') {
  resetGpu()
  const words = await renderComponent(p.ShaderCanvas, {
    file: { name: 'shader.frag', resolveUrl: async () => dataUrl },
    toolName, args: toolName === 'shader_render' ? { source: GOOD, name: 'strobe' } : { action: 'play', id: 'strobe' },
    result, fallback: { type: 'fallback' }, sessionId: 's1'
  }, fakeCanvas())
  return { words: [...new Set(words)], loop: frames.queue.length > 0 }
}

await check('the card plays a render whose flash verdict is none by itself', async () => {
  const seen = await card(renderResult({ verdict: 'none' }))
  equal([seen.words.includes('Live'), seen.loop], [true, true], JSON.stringify(seen))
})

await check('the card holds a flash-warn render on a still frame, as the page and the Lab do', async () => {
  const seen = await card(renderResult({ verdict: 'warn', max_general_per_s: 3 }))
  equal([seen.words.includes('Live'), seen.loop, seen.words.includes('Still frame')], [false, false, true], JSON.stringify(seen))
  equal(p.playGate({ autoplay: false, flash: 'warn' }).word, 'May flash', 'page gate')
})

await check('the card holds a flash-block render and a result without a flash verdict', async () => {
  for (const flash of [{ verdict: 'block' }, undefined]) {
    const seen = await card(renderResult(flash))
    equal([seen.loop, seen.words.includes('Live')], [false, false], `${JSON.stringify(flash)}: ${JSON.stringify(seen)}`)
  }
})

await check('the card reads the flash verdict from a play result as an object and as a string', async () => {
  const play = { success: true, id: 'strobe', version: 1, name: 'strobe', sliders: [], tuned_values: {},
    checks: { status: 'ok', flash: { verdict: 'none' } }, media: 'MEDIA:/data/shader.frag' }
  for (const result of [play, JSON.stringify(play)]) {
    const seen = await card(result, 'shader_library')
    equal(seen.loop, true, typeof result)
  }
})

await check('under reduced motion even a flash-free card shows a still frame', async () => {
  motion.reduced = true
  const words = await renderComponent(p.ShaderCanvas, {
    file: { name: 'shader.frag', resolveUrl: async () => dataUrl },
    toolName: 'shader_render', args: { source: GOOD, name: 'strobe' },
    result: renderResult({ verdict: 'none' }), fallback: { type: 'fallback' }, sessionId: 's1'
  }, fakeCanvas())
  motion.reduced = false
  equal([words.includes('Still frame'), words.includes('Live')], [true, false], JSON.stringify(words))
})

await check('Delete or Backspace on a focused slider resets that one slider', () => {
  const resets = []
  const slider = { name: 'glow', min: 0, max: 2, default: 0.8, step: 0.01, label: 'Glow' }
  const row = p.SliderRow({ slider, value: 1.4, onChange() {}, onBlur() {}, onReset: () => resets.push('glow') })
  const input = row.props.children.find(child => child && child.type === 'input')
  let prevented = 0
  for (const key of ['Delete', 'Backspace', 'ArrowLeft']) {
    input.props.onKeyDown({ key, preventDefault: () => { prevented += 1 } })
  }
  equal([resets, prevented], [['glow', 'glow'], 2], 'resets')
  equal(input.props['aria-keyshortcuts'], 'Delete', 'announced shortcut')
})

finish()
