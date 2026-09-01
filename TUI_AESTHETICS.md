# Son of Anton TUI — Aesthetic Overhaul

Two-track plan. **Phase 1 (done, this change)** is a comprehensive polish of the
existing `prompt_toolkit` TUI. **Phase 2 (scoped, not started)** is an optional
rebuild of the interactive shell as a Textual app.

The one visual anchor that stays in both phases: the ASCII `SON OF ANTON`
wordmark (its block-char art is never edited — only its applied style changes).

---

## Phase 1 — polish the prompt_toolkit TUI (DONE)

Direction (from the user): **terminal-native colors, styling/visual content up
to the agent.** That means the chrome rides the user's terminal theme instead of
imposing a palette, and the design energy went into hierarchy, glyphs, and
structure.

### What changed

- **`son_of_anton_cli/skin_engine.py` — `default` skin redesign.**
  - Palette is now terminal-native: every foreground is a `default`/`bold`/`dim`
    + named-ANSI token (`yellow`, `green`, `red`, `cyan`) rather than a fixed hex,
    so the terminal's own palette (kitty, etc.) decides the final hue. The
    prompt_toolkit chrome already re-snaps named colors onto the ANSI palette via
    `snap_pt_style_to_theme`, so this is native end to end.
  - Dark-dock surfaces (status bar, completion menu, voice) keep a persistent
    dark fill + light text so they read in BOTH terminal polarities with no
    light-mode remap.
  - Refined hierarchy: `banner_border` → `dim yellow`, `banner_title` → `bold`,
    `banner_dim` → `dim`, `ui_label` → `bold`, `input_rule` → `default`, and a
    new `ui_thinking` accent → `cyan`.
  - New calm, modern `spinner` (soft breathing faces + refined verbs) instead of
    noisy kawaii.
  - Cleaner branding strings (welcome, `help_header` → `Commands`).
  - The ASCII wordmark **constants are byte-identical** (guarded by
    `tests/test_banner_logo.py`).

- **`son_of_anton_cli/banner.py` — tint the wordmark.** `console.print(_logo)`
  now applies the skin's `banner_accent`, so the wordmark reads as brand (gold)
  instead of flat default text. The art itself is untouched.

- **`cli.py` — reasoning accent + centered boxes.**
  - Added `_THINKING = _SkinAwareAnsi("ui_thinking", "default")` and used it for
    reasoning-box content, so model thinking is now a distinct color (cyan on the
    default skin) while skins that don't set `ui_thinking` keep native reasoning.
  - The response box (`╭─ ⚛ Son of Anton ─╮`) and the reasoning box (`┌─ Reasoning ─┐`)
    are now **center-aligned** — the run of dashes is symmetric instead of one
    short dash + a long tail.

- **`son_of_anton_cli/cli_commands_mixin.py` — live skin switch.**
  - `/skin` now also resets `_THINKING`, so a live switch re-resolves the
    reasoning accent (matching the existing `_ACCENT.reset()`).

### Verified

- `scripts/run_tests.sh` — **45 files, 566 tests, 0 failed** (includes
  `test_skin_engine.py` and `test_banner_logo.py`).
- Import smoke-pass of `cli.py`, `banner.py`, `skin_engine.py`.
- Rendered the welcome banner at width 120: wordmark tinted, dim-gold panel, all
  chrome snapped terminal-native (`bg:ansiblack`, `default`/`dim` text).

---

## Phase 2 — Textual rebuild (SCOPED, optional follow-up)

The idea: replace the prompt_toolkit interactive shell with a **Textual** app —
a real application frame rather than an input line + scrollback. Textual is
Python-native and Rich-based, so it fits this Nix-first / Python codebase without
pulling in the Node/esbuild stack the removed hermes Ink TUI required.

### Why Textual (not Ink)

- **Python-native** — no Node, no esbuild, no JS bundle. Stays in the uv2nix
  toolchain and keeps `nix build` self-contained.
- **Rich-based** — shares a rendering lineage with the existing banner, diffs,
  and tool previews, so the terminal-native palette transfers cleanly.
- **Real widgets** — panels, data tables, tabbed views, spinners, modals, a
  command palette, docked layouts, CSS-driven theming. This is what makes it
  look like a genuine application rather than a REPL.
- **Async/reactive** — fits streaming tokens and background tool calls.

### Rendering capabilities (Markdown / LaTeX)

Pin this early, because the physics mode produces a lot of mathematical
derivations.

**Markdown — native, and streaming-able.**
- Textual ships a first-class `Markdown` widget (`from textual.widgets import
  Markdown`): headings, bold/italic, lists, blockquotes, links, tables, and
  fenced code blocks with syntax highlighting.
- Since **Textual 4.0** (2025) it supports streaming markdown, which maps
  directly onto the existing `_stream_delta` callback: tokens append into the
  widget instead of re-rendering the whole transcript every token.
- Rich (already a dependency here) also renders markdown, usable inline in a
  `RichLog`.

**LaTeX — no first-class support.**
- Neither Textual nor Rich has a LaTeX/math renderer and there is no `Latex`
  widget; nothing typesets real math (`∫₀^∞ e^{-x²} dx = √π/2`) in a terminal.
- Workarounds, in order of practicality:
  1. Show the LaTeX source in a fenced code block with syntax highlighting —
     cheap, always works, and correct while streaming.
  2. Plain-text/Unicode approximation via `pylatexenc.latex2text`
     (`\frac{a}{b}` → `a/b`) — readable, no images, no heavy dep.
  3. Render a discrete formula to a bitmap (matplotlib `mathtext`, or LaTeX→PNG)
     and show it via Textual's `Image` widget. Opt-in only: lossy, terminal
     scaling/contrast is fickle, and it breaks the streaming flow — wrong for a
     live transcript.

**Decision (reinforced):** markdown is a native win; math should be a deliberate
"source-block + latex2text" default with an opt-in image path — never assume LaTeX
renders natively.

### Reuse, don't rewrite

The entire agent stack stays as-is and is *not* touched:

- `AIAgent` / `run_conversation()`, `model_tools`, `toolsets`, `run_agent.py`.
- Skills, memory, delegation, cron, session store, `son_of_anton_constants`,
  `son_of_anton_logging`, the three-mode router, config, providers.
- `agent/display.py` — tool previews, friendly verb labels, inline diffs are pure
  functions already; feed them into Textual widgets rather than re-deriving them.
- `son_of_anton_cli/skin_engine.py` — expose the resolved palette to Textual
  (the engine already has a `resolve_skin`-style surface for the desktop/TUI);
  reuse `snap_pt_style_to_theme` semantics so colors stay terminal-native.
- `stream_single_writer` / thread-scoped output — reuse for the transcript so
  parallel tool output never interleaves.

### Architecture

- New module (e.g. `son_of_anton_tui/`) with a `TUIApp(App)` subclass, launched
  by `cli.py` when `display.interface: tui`.
- The synchronous `AIAgent` loop runs in a worker thread / `ThreadPoolExecutor`; a
  `queue` + `call_from_thread`/`run_worker` pumps its existing stream callbacks
  (`_stream_delta`, `_on_reasoning`, tool-progress) into Textual's reactive state.
- Layout via Textual CSS: header, left session/context panel, central streaming
  transcript (`RichLog`/`ScrollView`), right status/usage panel, bottom `Input`
  dock + `Footer`.
- Streaming: translate the callback deltas into transcript writes with the skin
  palette, reasoning as a collapsible section, tool calls as an inline timeline.

### UI surfaces to port from the current TUI

- Streaming / non-streaming response rendering (centered response box).
- Reasoning box (collapsible, `ui_thinking` accent).
- Tool-call progress + progress spinner line.
- Status bar → a footer/status dock (model, duration, goals, background
  process/subagent counts, session badge, YOLO, battery).
- Completion menu → command palette / fuzzy completer.
- Approval / clarify / model-picker / secret-input modals.
- Slash-command registry integration (autocomplete + dispatch).

### Risks / open questions

- **Threading + a single transcript writer.** The agent loop is synchronous and
  blocking; Textual is async. Must use a single writer + queue (reuse
  `stream_single_writer`) and `call_from_thread` to avoid interleaving tokens
  from parallel tools.
- **Streaming render throughput.** Textual `RichLog.write()` batches; verify the
  refresh cadence stays smooth on long turns (measure against the current
  prompt_toolkit path).
- **Parity gate.** Keep `display.interface: prompt_toolkit` as the default;
  Textual is opt-in until it reaches parity. Never delete the prompt_toolkit path
  in the same change.
- **Prompt caching is sacred.** Textual is purely a front-end; the system prompt,
  toolsets, and past context must stay byte-stable — do not route any
  model-facing mutation through the UI layer.
- **What happens to the Ink-era "tui-widgets" / petdex?** They were removed with
  the Ink TUI. Decide whether to port any (e.g. pet mascot) as Textual widgets or
  drop them. Recommend: drop for now, revisit after core parity.
- **Backward-compat / tests.** Textual supports `App.run_test()` + pilot; add
  layout snapshot tests and a real `AIAgent` smoke test against a temp
  `SON_OF_ANTON_HOME`.

### Phased plan

1. **Spike** — a standalone Textual app rendering a static transcript + input
   dock, wired to the skin palette. Validate streaming render perf.
2. **Live turn** — worker-thread loop + queue → token streaming, tool-call
   timeline, collapsible reasoning. Reuse `agent/display.py` builders.
3. **Port interactive widgets** — approval / clarify / model-picker /
   command-palette / session-switch as Textual screens/modals.
4. **Gate it** — enable behind `display.interface: tui` (and/or a `/tui` toggle),
   keep prompt_toolkit the default; add a parity smoke test.

### Status

Phase 2 step 1 (the spike) is **done and proven** — see
`son_of_anton_tui/tui.py`, which demonstrates streaming markdown (headings,
bold/italic, fenced code, tables), the LaTeX-source-in-a-code-block default, an
application frame (header / transcript / input dock / footer), and a skin-palette
hook. It was run headlessly against Textual 8.2.8 and verified. The dependency
is registered policy-correctly: opt-in `tui` extra + `tui.textual` LAZY_DEPS
entry + regenerated `uv.lock`, deliberately excluded from `[all]` during the
parity gate. `tests/test_tui_spike.py` guards the import-never-crashes contract
and runs the streaming demo when Textual is present.

Remaining phase-2 work is in the plan below.

### Decision needed before starting

- Confirm Textual replaces the Ink TUI at `display.interface: tui`.
- Confirm whether to port any Ink-era widgets (recommend no, initially).
- Confirm the retirement stance for the prompt_toolkit path (recommend: keep it as
  a fallback until Textual reaches parity).
