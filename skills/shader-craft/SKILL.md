---
name: shader-craft
description: Use when making, fixing, tuning or exporting a shader with Shaderbrücke.
---

# Shader craft with Shaderbrücke

You have `shader_render`, `shader_library` and `shader_export`. This skill is the loop and the
craft rules; the tools carry the schema.

## The loop (never skip a step)

1. **Write** `void mainImage(out vec4 fragColor, in vec2 fragCoord)` plus helpers. Shadertoy
   conventions: `iResolution`, `iTime`, `iTimeDelta`, `iFrame`, `iMouse`, `iDate` are injected.
   Never write `#version`, `precision`, your own `main()` or a uniform declaration. Constant loop
   bounds only: an unbounded loop is killed at the deadline.
2. **Render** with `shader_render`.
3. **Read the checks**, not just `success`: `checks.flat`, `checks.flash.verdict`,
   `checks.motion.verdict`, `checks.frames[].nan_fraction`, `checks.lint`. `flat: true` means the
   frame is one colour or black; `flash.verdict: block` means no autoplay and no `MEDIA:` line.
   Motion is `checks.motion` (`moving` or `still`, from the share of pixels that changed between
   frames), never `luma_mean`: drifting clouds keep the mean identical while a fifth of the
   pixels move. `slow: true` means the three frames look alike but frames at t = 10 or 30 s
   differ: the shader moves, just slowly. Do not rewrite it for motion unless the user wants
   movement visible within seconds.
4. **Look at the frame.** Call `vision_analyze` on `contact_sheet` (three frames side by side) and
   answer for yourself: is the thing I described actually in the picture? Does it move between the
   frames? Is there banding, clipping or a black corner? A render you have not looked at is not a
   result. This host does not put tool images into your context (probe P7), so the call is yours
   to make.
5. **Fix and render again** until the checks and your eyes agree. Budget:
   three `shader_render` calls per request, failed ones included, unless the user names another
   number. When the checks pass and the picture shows what was asked, stop and show it; offer
   further polish in words instead of rendering it. From the third version within 15 minutes the
   result's `next` says "Stop here", and a fourth version is refused (`render_budget`, with the
   latest version's `media` line to show) unless the user asked for more and you pass
   `keep_iterating=true`. After a failed render, call again with the same `id`: a failed first
   render becomes version 1, and a failed fork keeps its parent.
6. **Only then** put the `media` line on its own line in your reply, so Desktop plays it live. If
   the result has no `media` key, say why (flat, flash-blocked, compile error) instead of
   pretending it works.

A `MEDIA:` line becomes the live card (canvas and sliders) only when it comes from a
`shader_render` result or a `shader_library play` or `import` result. Never copy a path from `get`
or type one yourself: Desktop then shows a plain file with no sliders. To show or tune a version
that already exists (an `@shader:` reference, "play it", "let me tune it"), call
`shader_library play` with its id and version, never a new render.

Report the numbers you saw (`luma_mean`, `luma_std`, `checks.motion`, flash verdict), not adjectives.

## Tunables are sliders

Every number a person may want to change becomes a slider annotation:

```glsl
// @slider cloudCover 0.0..1.0 = 0.55 step 0.01
```

Rules: identifier names only; never a GLSL keyword or an injected uniform; at most 16; ranges must
ascend and the default must sit inside them. Sliders become uniforms in every export and sliders in
Desktop, so the user tunes instead of re-prompting you. An optional quoted label names the slider
for people: `// @slider sunHeight 0.05..0.95 = 0.32 step 0.01 "Sun height"`.

The annotation is the declaration: the renderer adds `uniform float cloudCover;` before your lines.
Use the name as it is. Never declare it yourself (`const float cloudCover = 0.55;`, a `uniform`, a
`#define`): that fails to compile, and the lint rejects it as `slider-redeclare`. If
`shader_render` ever answers `'cloudCover' : undeclared identifier`, the loaded plugin is older
than this skill; say so instead of pinning the value as a constant.

## The human's tuned values

When the user moves a slider in Desktop, the value is saved as `tuned_values` on the version the
card plays (the Shaders page tunes the latest version). Every export, the handoff and the proof use the effective value: the tuned value when
there is one, else your default. Export headers show both (`default 0.32, tuned 0.41`).

- Before an export or a new version, call `shader_library("get", id)` and read `tuned_values` and
  `effective_values`.
- When you write the next version, keep the tuned values as the new `@slider` defaults unless the
  user asked for a different look. The new version starts with no tuned values of its own.
- If `shader_render` returns `previous_tuned_values`, your new source dropped the human's tuning.
  Put those numbers back as defaults and render again, or say why you did not.

## Versions, forks, library

- `shader_render(id=...)` writes the next version of an existing shader. An unknown `id` is an
  error: omit `id` for a new shader. The same source as the latest version writes nothing and
  returns that version with `unchanged: true`.
- `shader_render(parent_id=...)` forks: a new id whose `provenance.parent` points at the original.
- `shader_library("list")` shows the newest 20, with `q`, `state`, `tag`, `starred`, `archived`
  filters and a `cursor` for the next page. It is the same library as Desktop's Shaderbrücke page.
  `get` returns source, sliders, `tuned_values`, `effective_values` and the checks; `fork` copies
  one (pass `version` to fork an older version).
- `rename`, `tag` (the full new list) and `archive` / `unarchive` organise the library when the
  user asks. There is no delete: the user deletes in Desktop.
- A message can carry `@shader:<id>` or `@shader:<id>@v<n>`: the source, sliders, tuned values,
  checks and lineage are already in the message. Do not call `get` for it again; to play it, call
  `shader_library play`.

Fork instead of overwriting when the user wants to compare two looks.

### The user edits too

The user can edit a shader in Desktop's Lab and save it as a version with `author_kind: human`.
When that happened since your last turn, the turn starts with one line naming the shader and its
new version. Read that version with `get` before you change the shader, build on their edit, and
never quietly undo it. Every version you render after theirs is yours again (`author_kind: agent`).

## Exports

`shader_export(id, target, dest_dir=None, overwrite=False)`. The proof verdict is the truth:
`match` means the exported code renders like the source, `differs` means it does not, `not_proven`
means it could not be checked (and the `why` says what failed). Never call an export done on
`differs` or `not_proven`. The proof compares at t = 0.5, 1 and 2 s, once at the effective slider
values and once with every slider at a quarter of its range; `proof.checks` lists every
comparison, so a slider the export ignores or a shifted clock shows up as `differs`.

- `three`: `<Name>Material.js` (factory) + `<Name>.frag.glsl`. `create<Name>Material()`, then
  `material.setSize(w, h)` and `material.update(seconds)`.
- `r3f`: `<Name>.tsx` + the material module it imports. Props are the sliders.
- `shadertoy`: one paste-ready Image tab; sliders become `const float`. The tab itself is rendered
  and compared with the source.
- `tsl`: `<Name>.tsl.js`, generated by three r187's own transpiler and rewritten deterministically.
  **Never write TSL yourself** and never hand-edit the module: if it reports `differs`, say so.
  It refuses `#if`/`#ifdef` blocks, directives other than `#define`/`#undef`, and any quote,
  backslash or non-ASCII character outside comments; to export such a shader, render a new
  version without them (keep the branch you use).
- `agent`: a Markdown handoff block for another agent or a teammate, plus `reference-t1.png`. Its
  proof is the three proof of the module embedded in the block. The block ends with an acceptance
  check of plain numbers (mean RGB of a 3x3 grid at t = 1 s), so the receiving agent needs no
  access to this host. Send the PNG along with the block.
- `td`: `<Name>.td.glsl`, a TouchDesigner GLSL TOP pixel shader. `isf`: `<Name>.fs`, an ISF 2.0
  generator for VDMX, Resolume Wire and the ISF Editor. See the next section.

## TouchDesigner and ISF

Both targets are `experimental`: each file is rendered in a WebGL2 shim of its host's wrapper
(`td-shim`, `isf-shim`) and compared like the other targets, but neither TouchDesigner nor an ISF
host has rendered it yet. On `match`, say "matches in the shim, not yet checked in the host"; never
say it works in TouchDesigner or VDMX.

- **Time in TouchDesigner is a uniform the user binds.** The GLSL TOP has no clock: the file
  declares `uniform float iTime;` and the user adds a Vectors page row `iTime` with the expression
  `absTime.seconds` (`me.time.seconds` follows the timeline). Each slider is a `uniform float` with
  its own row at the tuned value; an unbound uniform reads 0, which looks like a broken shader.
  `iResolution` comes from `uTDOutputInfo`, so it needs no row. Put every row from the steps in
  your reply.
- TouchDesigner adds `#version` itself (a `#version` line is an error), wants the output size set
  on the Common page (Custom Resolution), and shows a checkerboard on a compile error: the log is
  in the GLSL TOP's info DAT. Keep the pixel format at 8-bit fixed or float, not sRGB 8-bit.
- **ISF hosts declare the uniforms.** TIME, RENDERSIZE, isf_FragNormCoord and one uniform per
  INPUT come from the host; declaring any of them again is a compile error. The sliders are float
  INPUTS whose DEFAULT is the tuned value. The JSON comment must be the first thing in the file.
- ISF hosts on an OpenGL 2.1 context compile GLSL 1.10, where ES 3.00-only syntax (`uint`, bit
  operations, `round`, `tanh`, `switch`, `texture()`) fails; say so when the user's host is old.
- No audio yet: ISF `audioFFT` inputs come in a later version. Channel inputs are exported only for
  shaders that have channels, and their proof says `not_proven` because the reference has none.

`dest_dir` writes into a project only when the user asks for it and the setting allows it; never
overwrite without `overwrite=true`. The copy happens after the proof and only on `match`. Pass
`accept_unproven=true` only when the user accepts an export that is not proven, and tell them so.

## Provenance and licence

Every export header carries origin, author, source URL and licence. If the user imported a shader,
its licence travels with it; an NC licence is inherited, not sellable. Never strip those lines and
never relicense someone else's code.

- `set_licence` only on the user's request: SPDX `MIT`, `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`,
  `CC-BY-NC-4.0`, `CC-BY-NC-SA-4.0`, `CC-BY-NC-SA-3.0` or `private`. A refusal names the licences
  that are allowed; pass that on, do not look for a way around it.
- `import` brings in a share file (`path`, inside the plugin's data folder) or a pasted shader
  (`source`, plus `url`, `author` and `licence` when the user names them). Shadertoy, glslCanvas and
  single-pass ISF generators are converted; multipass, textures, audio and image inputs are refused
  with the reason. Nothing is fetched: a URL alone is not an import, ask the user to paste the code.
  A pasted Shadertoy shader without a named licence gets Shadertoy's default, CC BY-NC-SA 3.0, and
  the answer warns about it. Tell the user every warning, the licence one first.
- `share` writes `<name>.shaderbruecke.json`: source, sliders, tuned values, licence, author,
  lineage and a thumbnail. Anyone with Shaderbrücke imports it. The claims in a share file are the
  sender's claims, not checked facts.

## Look and craft

- Comment for a reader, not for the compiler: one line above a block that says what it does.
- Build in layers (background → subject → light → grain) so each render tells you which layer is
  wrong.
- Prefer `smoothstep` with ascending edges; inverted edges are undefined and render black through
  TSL.
- Avoid `pow` of a negative base, division by a value that can be zero, and `length()` of a huge
  vector; each turns into NaN and a black frame. Clamp and `max(x, 0.0)` first.
- Keep per-pixel work bounded: 64 to 128 steps for a raymarch at 512x288.
- A little dither (`fract(sin(dot(...)))`) hides 8-bit banding in gradients.

## Learning mode

When the user asks to learn, or asks "why does this line do that":

1. Name the exact lines (quote them) and explain in plain words what they do and why they are
   there. No jargon without a translation.
2. Tie the idea to a slider: which one changes it, and what to watch when it moves.
3. Offer one small exercise with a hint, not the answer. Example: "lower `cloudCover` to 0.2 and
   tell me what happens to the horizon".
4. If they try something, render their attempt and describe what changed between the frames.
5. Never replace the learner's code without asking. They are the author; you propose.

## When something is wrong

- `lint_error`: fix every listed line; the hints name the ES 3.00 form.
- `compile_error`: the log uses the agent's line numbers; `source_excerpt` shows them numbered.
- `flat: true` after a successful compile: `fragColor` is not written on every path, or it is
  written with a value that clamps to zero.
- `flash.verdict: block`: more than three general flashes per second; slow the animation or lower
  the contrast between states.
- `timeout`: the result names its `phase`. `frames` or `flash` means the shader is too slow per
  pixel, usually an unbounded or very long loop; use a constant bound. `compile` means very long
  unrolled loops or deep nesting. `start` means the host was busy: render again.
- `busy`: other renders for this profile are running (at most two at once). Render again in a
  moment; a result that says it waited is normal under load.
- A failed render is not a version. It stays in the scratch folder; the next attempt with the same
  `id` reuses the version number.
