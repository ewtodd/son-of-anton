# Son of Anton TUI — the Textual interface

Textual **is** the interface. `son-of-anton` with no subcommand launches it;
there is no flag, no config switch, and no second front-end. The prompt_toolkit
REPL that preceded it — `SonOfAntonCLI.run()`, its key bindings, layout,
status-bar renderer, modal panels, terminal-mode repair and CPR/paste patches —
was deleted in 0.3.0, along with the `prompt_toolkit` dependency itself.

The one visual anchor carried across from it: the ASCII `SON OF ANTON` wordmark
(its block-char art is never edited — only its applied style and which variant
fits).

## How it fits together

- **`son_of_anton_tui/backend.py` — `TextualBackend(SonOfAntonCLI)`.**
  `SonOfAntonCLI` is no longer a front-end; it is the session/agent lifecycle
  (credentials, routing, persistence, slash commands, interrupts, the modal
  prompts) with the *rendering* seams left open. The backend overrides those
  seams: streamed tokens, reasoning, tool lifecycle and `_cprint` /
  `ChatConsole` / bare `print` become typed events; the queue-based prompts
  (approval, clarify, sudo, secret, confirm, model picker) are answered by the
  app through the same queues; `finish()` carries the whole shutdown sequence
  the old `run()` did in its `finally`.
- **`son_of_anton_tui/tui.py` — the app.** Layout, feed widgets, modals,
  completion, key bindings.
- **`son_of_anton_tui/palette.py` — terminal-derived surfaces.** See below.

Two things the old input loop drove are now driven by the app: `idle_tick()`
(config watch for `mcp_servers`, background-process notifications, due `/loop`
wakeups) runs on the app's timer, and `_post_turn_hooks()` (turn footer,
interrupt drain, standing-goal judge, `/loop` completion) runs at the end of
every turn.

**Anything that draws its own screen must suspend the app first.** An external
editor cannot share the tty with a full-screen app: both write to it and both
read stdin, so the child's output and its escape-sequence replies surface as
stray characters. `TextualBackend.run_with_terminal()` hops to the app thread
and wraps the call in `App.suspend()`; `_compose_in_editor` (used by `/prompt`
and by ctrl+g) goes through it. The same reasoning is why
`son_of_anton_constants.is_frontend_active()` exists: `tools/approval.py` and
`tools/lazy_deps.py` must never fall back to a bare `input()` while the app
holds stdin.

---

### Layout — mirroring opencode

The frame is a deliberate port of opencode's session route
(`packages/tui/src/routes/session/index.tsx`), read from source rather than from
screenshots. What we mirror, and where it comes from:

| opencode | ours |
| --- | --- |
| row of main column + 42-col sidebar; column holds `paddingLeft/Right 2, paddingBottom 1, gap 1` | `#split` → `#content` (padding `0 2 1 2`) + `#context` |
| sidebar visible when `sidebarOpen || (auto && width > 120)`; **overlays** the content when narrow rather than disappearing | same threshold; `.overlay` class docks it to the right layer |
| sidebar: bold title, muted session id, workspace, and the product + version pinned at the bottom | same, with our context/usage detail as a middle block |
| `TextPart` indents markdown by 3 | `#feed PlainMarkdown { padding-left: 3 }` |
| `UserMessage`: left rail in the agent colour, padding `1 0 1 2` | `UserTurn` with `border-left: wide $primary` |
| `InlineTool`: 2-cell icon column then the label; spinner occupies that column while running | `ToolLine`, icons per tool (`$` shell, `→` read, `✱` search, `◈` web, `←` write) |
| `Prompt`: left rail, textarea, then a meta row of `agent · model provider` | `#prompt-frame` + `#prompt-meta` |
| status row under the prompt: working directory when idle / spinner + action when busy, with usage and shortcut hints right-aligned | `#statusline` (`#status-left` / `#status-right`) |
| spinner frames `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` | identical |
| `setPreLayoutSiblingMargin` + `alwaysSeparate`: a row gets a blank line above it when the previous sibling was a block or was multi-line | the same rule, applied when a row is mounted |
| `generateSystem` derives panel/element surfaces from the queried terminal background | `palette.py` → `$panel` / `$surface` |

**Surfaces follow the terminal too.** opencode's "system" theme does not settle
for a single surface: it asks the terminal for its real default background
(OSC 11) and *generates* a gray ramp from it — `backgroundPanel` and
`backgroundElement` are the terminal's own colour nudged toward its opposite —
while leaving the base background transparent so terminal transparency and
background images survive (`generateSystem` / `generateGrayScale` in
`packages/tui/src/theme/index.ts`).

`son_of_anton_tui/palette.py` is that, ported. We query OSC 11/10 once at
startup, before Textual owns the tty, and generate `$panel`, `$surface`,
`$border` and `$text-muted` from the answer; `$background` is never painted.
So a user message, the prompt block and the sidebar each read as their own
surface and still belong to whatever theme the terminal runs. Polarity now comes
from the queried background's luminance rather than the `COLORFGBG` guess.

Two things stay on the ANSI palette by choice. Accents (`$primary`, `$secondary`,
…) remain `ansi_*` tokens so the skin's colours are the terminal's own. Text
stays `ansi_default`, which already *is* the terminal foreground and keeps
following it if the user retints mid-session; pinning it to the queried hex also
breaks blends that read `$foreground` (Textual draws markdown table keylines as
`$foreground 20%`).

When the terminal doesn't answer — not a TTY, a terminal that ignores the query,
`SON_OF_ANTON_TUI_NO_COLOR_QUERY=1` — every generated variable is simply absent,
surfaces stay transparent, and the rails carry the separation alone.

Not ported: opencode's separate Home route (centred logo with a 75-column prompt
before a session exists). Our wordmark opens the transcript and scrolls away
instead, which is the form the wordmark work already settled on.

### Keys beyond opencode's set

`shift+tab` cycles this **session's** permission mode (default → ask → lockdown
→ yolo). It is session-scoped on purpose: it never writes config, so it cannot
outlive the session that chose it, and `/new` or a new process starts from the
configured mode again. `/perm` remains the way to change the persistent profile
setting.

That required one change outside the front-end. `tools/approval.py` had
session-scoped state for yolo only (`_session_yolo`); everything else read
config. It now also carries `_session_permission`, a session-keyed override that
`_get_approval_mode()` and `_is_lockdown_enabled()` consult before config, and
that `clear_session()` drops with the rest of a session's approval state. The
override resolves to the same mode values config already produces, so it widens
nothing: the hardline block and the sudo-stdin guard run before any mode check
and still fire under a session `yolo` (guarded by a test). The prompt's meta row
shows any non-default mode, `yolo` in bold red, tagged `(session)`.

### What the app renders

Transcript in a bottom-anchored `VerticalScroll`; user turns with an accent rail;
streamed `Markdown` (via `MarkdownStream`, fenced code left unhighlighted); a
collapsible reasoning block that folds once the answer starts; one row per tool
call that goes running → completed without reflowing; the status row; the 42-col
context panel (`ctrl+b`); a multi-line prompt with inline slash completion
(`tab`) and Textual's palette (`ctrl+p`) over the slash registry; and modal
screens for approval, clarify (single / multi / batch / free-text), sudo,
secret, destructive-command confirm, the two-stage model picker, and generic
text + list prompts. `ctrl+c` interrupts a running turn (twice to force-quit);
`ctrl+g` composes in `$EDITOR`; a message typed mid-turn follows the session's
own interrupt/queue setting; `:q` quits.

### Verified

`tests/test_tui.py` covers the frame and its responsiveness, the feed event
stream, the transcript column geometry, slash completion, every modal, the
backend seams against the real `cli` module under a temp `SON_OF_ANTON_HOME`,
an end-to-end pilot turn whose stubbed `chat()` raises approval / clarify /
sudo prompts from a worker thread, interrupt and quit-mid-turn, the model
picker's two stages, the OSC round-trip over a pty, the generated ramp's
direction and readability, the editor handoff (including a driver that cannot
suspend, and an empty save), and the idle / post-turn hooks. Plus live runs
against the configured provider.

### Still open

Image attachments (`/attach`, drag-and-drop) are not surfaced in the prompt, and
the `/agents` spawn-tree viewer has no Textual equivalent yet. A few
prompt_toolkit-era overlays were replaced by Textual equivalents rather than
ported one-to-one (the command palette, the model picker's fuzzy filter).
