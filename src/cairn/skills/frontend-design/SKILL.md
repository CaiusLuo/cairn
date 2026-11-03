---
name: frontend-design
description: Design or refine web UI components, pages and HTML/SVG widgets. Load for frontend visual implementation; skip backend, CLI/TUI, general coding and ordinary chat.
---

# Scoped frontend design

Apply this guidance to the requested frontend deliverable only. A previous use
does not make it a rule for later unrelated tasks. Existing product requirements,
accessibility needs and the project's design system take precedence.

## Establish the host

Inspect the target components, theme definitions and installed dependencies.
Identify whether the output is a standalone application or a widget embedded in
another surface. Reuse the project's components, fonts and icon package. Cairn
does not provide Anthropic Sans, Tabler, predefined SVG classes, a browser
renderer, `read_me`, or `sendPrompt()`. Do not emit calls to unavailable APIs
or assume a stylesheet exists. Use the application's actual interaction handlers.

## Visual defaults

For a new, restrained interface without an established visual system:

- Prefer flat surfaces, restrained borders and whitespace over decorative
  gradients, shadows, glow, blur or textures. Keep functional focus indicators.
- Use semantic theme tokens for surfaces, text, borders and status colors;
  verify both light and dark appearances, including SVG labels.
- Start body text at 16px/1.7 and headings at 22/18/16px. Prefer weights 400/500;
  keep small labels at least 11px. Use sentence case.
- Start with 8px control and 12px card corners, subtle 0.5px borders and
  responsive columns. Fit the actual container rather than a fixed host width.
- Use existing outline icons, not emoji or improvised icon paths. Hide decorative
  icons from assistive technology and name icon-only controls.
- Keep diagram captions short: about five words, at most four full-size boxes
  per row and two color families. Explain semantic colors with a legend.
- In streamed widgets, render short styles, visible content, then scripts.
  Keep explanations outside the widget; avoid initially hidden sections.
  Use transparent outer surfaces only when the host supplies a background.
  Keep auto-height iframe mockups in normal document flow.

These are starting values, not a reason to restyle an existing product.
Application help text, form labels, error messages and required code comments
remain part of a maintainable implementation. Ordinary applications may use
tabs, fixed dialogs and other patterns appropriate to their own layout.

## Verify the actual deliverable

Exercise the changed interaction and inspect narrow and wide layouts, keyboard
focus, overflow, empty/error states and theme contrast using the project's
available tools. Report checks actually performed and any missing visual
inspection; a generated screenshot or a successful build alone does not prove
the controls work.
