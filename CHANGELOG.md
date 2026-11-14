# Changelog

## 0.2.6

- The 15-minute render budget counts every browser attempt, including failed compiles and attempts
  that produced no version. The durable ledger survives reconnects and counts a success once.
- `@shader:` references are capped at 6,000 characters. Large sources end with a truncation notice
  and a `shader_library get` route to the full text.
- The egress suite proves that a raw-IP HTTP fetch is intercepted before it reaches a server.

## 0.2.5

A review round on security and correctness before the catalog's next review. Update both halves: the plugin where Hermes runs and the one Hermes Desktop loads.

- Writes into a project folder (`shader_export` and `shader_library share` with `dest_dir`) refuse a
  symlink at the target and never write through one.
- A share file or a pasted ISF shader with a number that is not finite (1e400, NaN, a 400-digit
  integer) is refused before anything is stored.
- Saving slider values, resetting them or changing the licence while the shader is deleted no
  longer brings it back, and a licence change racing a new version stamps that version.
- Slider names that are JavaScript reserved words (`new`, `delete`, `function` ...) are ignored like
  GLSL keywords, so the R3F and TSL exports always parse; the R3F proof no longer claims the
  `.tsx` wrapper was rendered.
- Chromium keeps its sandbox wherever it can start one; only a host that refuses it (a container
  without user namespaces) runs with `--no-sandbox`, and a render result that is a new version
  says which (`browser_sandbox`).
- When Hermes does not say whether a person is present, project-folder writes are refused with a
  sentence asking you to update the plugin.
- Tool arguments of the wrong type (a NaN version, a list as a name, an infinite time) are refused
  with a sentence instead of an error.
- The proof page server no longer ends a proof on a request path with a NUL byte, and a browser that
  stops reading cannot hold a render past its deadline.
- `@shader:` references fence the source so a backtick run inside it cannot end the block.
- The pre-LLM hook checks one file instead of opening the database before every model call; it
  reads the database once per process, so a Lab save stored by 0.2.4 is still told to the agent.
- Slider saves while a thumbnail refresh runs join it instead of starting one Chromium each.
- Chromium or Google Chrome in a Mac's Applications folder is found without setting a path.
- Settings have labels, README and manifest describe the same settings, and the README discloses
  share-file writes, background thumbnail work and Desktop's session calls.
- The chat card plays by itself only a version whose flash check found nothing.
- "Share files" never replaces a file in the folder you picked; same-named shaders get `-2`, `-3`.
- The Shaders page asks for thumbnail batches three at a time and asks again for a batch that failed.
- "Load more" during a refresh no longer loses the loaded page.
- Delete (or Backspace) on a focused slider resets that slider.

## 0.2.4

Clearer action buttons on the shader view and the chat card, and the thumbnails on screen load first.

- Every action button on the shader view, its Export menu, the selection bar and the chat card has
  a role, drawn with Desktop's theme colours so light and dark both work. Edit in Lab is filled in
  the accent colour like Continue in chat. Share file, Fork, Export and the Copy, Send and Export
  now actions of the Export menu have a tinted fill and an outline. Archive, the More actions
  button and Show code have an outline. Delete stays red. Before, Edit in Lab, Share file and
  Archive had no outline at all and read as plain text.
- The shader view's second row of actions (Edit in Lab, Share file, Archive, More actions) starts
  at the same left edge as the row above and spaces its buttons 6 px apart like that row. Before,
  it started 8 px further left with 2 px gaps. The Export menu's Copy and Send buttons are 6 px
  apart too, instead of 2 px.
- The Shaders page asks for the thumbnails of the tiles on screen first, in a request of their
  own, and the rest of the page after them. Before, the tiles on screen waited for one request
  carrying the images of all 48 loaded rows.

## 0.2.3

Fixes from the catalog review (NousResearch/hermes-agent#154066).

- The proof page no longer gets `file://` access. It loads from the plugin's own `headless/`
  folder over the DevTools pipe, and every other request fails.
- The TSL transpiler reads the shader with comments removed and `#define` macros expanded, and
  only after the GLSL compiler accepted that exact text. The TSL export refuses a quote, a
  backslash, a non-ASCII character, `#if` blocks and other directives outside comments, so an
  imported shader cannot put JavaScript into the module. A macro call split over two lines now
  exports correctly.
- `@shader:` checks the running profile's config through `hermes_cli.config.load_config()` and no
  longer reads plugin-manager internals.
- A slow shader is no longer called a still. Besides the frames at t = 0, 1 and 2.5 s, every render
  now also draws t = 10 and 30 s for the motion check. A shader that only moves by then reads as
  moving, with `slow: true`, and the hint says it moves slowly instead of asking the agent to fix
  `iTime`.

## 0.2.2 and earlier

This file starts after 0.2.2. Earlier changes are described in the release commits of this
repository.
