# Changelog

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
