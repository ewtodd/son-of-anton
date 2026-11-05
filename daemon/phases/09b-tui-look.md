# Phase 9b: the Go TUI's look

Goal: `soa-d tui` looks like the Textual TUI: the sidebar, the wordmark,
markdown answers, skins, and surfaces drawn from the terminal's own
background.

Read first: `phases/09a-tui-core.md` (what is there), and the reference:
`son_of_anton_tui/tui.py` (T), `feed.py` (F), `palette.py` (P),
`son_of_anton_cli/skin_engine.py` (S), `son_of_anton_cli/banner.py`,
`agent/markdown_tables.py` (MT). Where this file and the Python differ,
this file wins. Next: 9c pickers, completion, palette, mouse, editor; 9d
the subagent view; 9e switching `son-of-anton` over.

## Already written (do not modify)

`internal/tui/look_test.go`, `internal/tui/testdata/markdown.json` (F's
`render_markdown_lines` output on a corpus, with the two fixes below),
`internal/tui/testdata/palette.json` (P's `build_palette` output).

## API additions

```go
func Markdown(src string, width int) []string // a markdown block's rows at this feed width, styled
func Palette(bg string) map[string]string     // P's build_palette for a #rrggbb background
func (a *App) Styled() string                 // Screen with its styling
// Options gains:
	Background string // the terminal's background as #rrggbb; empty: ask the terminal
```

`Styled` is the frame as painted. When `out` is not a terminal the app
renders with the true-colour profile, so colours read the same in tests.

## The feed

- A feed shorter than its area starts at the top; once it overflows it is
  anchored to the bottom as in 9a.
- **Wordmark** (T:184-202, F:374-400): the first block, before the intro,
  bold in the accent colour: `SON_OF_ANTON_AGENT_LOGO_WIDE` from banner.py
  when the feed's content width is at least 109, the STACKED form when at
  least 52, else the text `SON OF ANTON`. Every line has the same left pad,
  `(width - widest line) / 2`. Chosen again on every resize; it survives
  `ctrl+l` and `/new` draws it again.
- **Markdown** (F:184-282): assistant text and compaction summaries render
  through `Markdown`. It is F's renderer: headings bold (h1, h2 also in the
  accent colour); paragraphs with inline styles (code muted, em italic,
  strong bold, strike, links underlined, images `🖼 alt` muted, soft breaks a
  space, runs of whitespace one space, hard breaks a new line), padded 3;
  fenced code as `▎` + the line, muted, from column 0, cropped at the width
  minus 3 (never wrapped); tables through MT's `realign_markdown_tables` at
  the width minus 6, every row padded 3 and cropped; bullet and ordered
  lists with `• ` / `N. ` markers in the thinking colour and a hanging
  indent; blockquotes as a `▎` in the thinking colour then the text; a rule
  as `─` across the width, muted; HTML blocks raw. No blank rows between a
  block's parts, as in F. Re-rendered on resize and on each streamed flush.
  Where F and this file differ (the fixtures pin these):
  - A fenced line longer than the row is cropped (F wraps it).
  - Every row of a realigned table is padded 3 (F pads only the last row
    of a line it had to wrap).
  - In the vertical form MT uses for a table too wide for the room, a label
    whose value's first word does not fit beside it ends its line at the
    `:`, and the value continues below with the 2-column indent (MT lets
    that line run past the width).
  - Nested list items are drawn, each under its parent and indented 2 more
    columns, with its own list's marker (F drops them). An indented code
    block draws like a fenced one (F drops it).

## The sidebar (T:1146-1155, 1303-1337, 1386-1437, 1740-1811)

42 columns at the right edge of the screen, outside the content inset: a
`│` in the accent colour in its first column, then 2 columns of padding,
on the panel colour, from the top row to the bottom row; 1 blank row at the
top and the bottom. Rows, each cut with `…` to fit:

- the title (bold; `untitled` when empty), the session id (muted), the cwd
  with `~` (muted);
- labels (muted, a blank row before each) with their values: `mode`;
  `reasoning` (the effort or `default`); `context`: the bar of 10 cells,
  `▰` filled for `round(pct / 10)`, `▱` the rest, then `  <pct>%`, in the
  accent colour, and below it `<tokens> / <length>` (`1.2K`, `3.4M` as
  T:235) plus ` · <N> compacted` when there were compactions; without a
  context length, `▱▱▱▱▱▱▱▱▱▱  —` muted and `no turns yet`; `session
  usage`: `in <in> · out <out> · <calls> calls`; `todo`: the latest `todo`
  tool result's complete checklist — a summary row (`<N> pending · <N>
  completed`, counting in the order pending, in_progress, completed,
  cancelled), then per item a glyph and the text (`▶` the in-progress one
  in the accent colour, `✓` completed in the ok colour, `✗` cancelled in
  the error colour, `·` the others muted), `+N more` when the rows run
  out, `none` before any todo call; `background`: `<N>`
  processes`, the label and row only while some run;
- then muted `session <duration>` since the session was created (`45s`,
  `12m`, `3h 5m`, `2d 4h`);
- pinned to its last row before the bottom pad: `•` in the ok colour, a
  space, `Son of ` and `Anton` bold, a space and the version.

Shown when the screen is wider than 120 columns, then the feed's room is
the width minus 42. `ctrl+b` toggles it; after the first toggle a resize
never shows or hides it again. Toggled on at 120 columns or fewer, it is
drawn over the right 42 columns, and the feed keeps the full width beneath.
Refreshed with the status, every second while busy and on every event.

## Surfaces (P:85-239)

At start the app asks the terminal for its background (OSC 11, 120ms
timeout; not asked when `out` is not a terminal or
`SON_OF_ANTON_TUI_NO_COLOR_QUERY=1`), unless `Options.Background` gives it.
`Palette` is P's `build_palette` (`gray_scale`, `muted_text`, `luminance`,
`polarity`): `panel` is the user rows' and the sidebar's background,
`surface` the prompt frame's, `text-muted` muted text's colour and `backdrop`
the dim behind an open dialog. Without a background, 9a's adaptive greys and
faint text stay.

## Skins (S:158-194, 225-760, 830-940)

- The nine built-ins with S's names and descriptions (`default`, `ares`,
  `mono`, `slate`, `daylight`, `warm-lightmode`, `poseidon`, `sisyphus`,
  `charizard`) and S's six colour keys: `ui_accent` (rails, wordmark,
  headings, the sidebar border, modal borders), `ui_thinking` (reasoning,
  list markers, quote rails), `ui_tool` (tool icons), `ui_ok` (the footer
  dot), `ui_warn` (the approval border), `ui_error` (tool errors and error
  notes). A key a built-in leaves out comes from `default`.
- User skins: `<home>/skins/*.toml`, `name` (else the file's stem),
  `description`, and a `[colors]` table of those keys; missing keys come
  from `default`. A user skin named like a built-in replaces it.
- A value's first colour word (`black red green yellow blue magenta cyan
  white`, `gray`/`grey` = bright black) is that ANSI colour (other words,
  like `bold`, drop); `default` is the terminal's own; `#rgb` and `#rrggbb`
  as given; anything else falls back to `default`'s value (T:117-141).
- At start: `[display] skin` in config.toml (`internal/config` gains it),
  else `default`; an unknown name is `default`.
- `/skin`: the note `skin: <current>`, then per skin `  ● <name> — <desc>`
  for the current one and `    <name> — <desc>` for the rest, built-ins in
  S's order then user skins by name. `/skin X`: everything is drawn again in
  X at once, the note `skin: X`; an unknown X: `unknown skin: X`. It is not
  saved (config.toml is Nix-managed). `/help` lists `/skin [name]`.

## Decisions made without the owner

- The two F bugs and the MT overflow above are fixed, not copied; nested
  lists and indented code are drawn rather than dropped.
- No blank rows inside a markdown block, as today: changing how answers
  read is the owner's call.
- `/skin` is not saved, as today.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with the race run of
`./internal/client/ ./internal/tui/`), `ruff check .` and `ruff check
--select F401,F811,F821,F841 .`, `scripts/run_tests.sh`, and `nix flake
check`. One commit: summary `daemon: phase 9b (Go TUI look)`, body listing
what changed, ending with the `Model:` line.
