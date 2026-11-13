# Changelog

## 0.2.4

Feedback round of 2026-11-12.

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
