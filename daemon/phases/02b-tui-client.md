# Phase 2b: the Textual TUI on soa-d; `-q` and `-z` on soa-d

Goal: `son-of-anton` (interactive) runs the existing Textual app as a client
of soa-d, and `-q` / `-z` run `soa-d query` / `soa-d oneshot`. The Python agent
stays only where something else still needs it: `chat -Q` (the Python
gateway's relay workers), the gateway, cron, and every other subcommand.

Owner decisions (2026-11-02): the daemon-backed TUI is the default, with no
opt-in flag and no fallback to the Python backend; `-q` and `-z` switch in the
same phase.

Read first: `phases/02a-server.md` (the protocol; it is the contract this
client speaks), `SPEC.md` section 5, the TUI (`son_of_anton_tui/tui.py`,
`feed.py`, `completion.py`), and the backend being replaced
(`son_of_anton_tui/backend.py`). The repo's top-level `AGENTS.md` rules apply
to the Python side (`scripts/run_tests.sh`, never bare pytest; ruff).

## Already written (do not modify)

- `tests/test_tui_daemon.py`: the app, the client and the entry point against
  the real `soa-d` binary and a scripted fake model server.
- `daemon/cmd/soa-d/reasoning_test.go`.

## soa-d

`query` and `oneshot` gain `--reasoning LEVEL`, which replaces the resolved
reasoning effort.

## son_of_anton_tui/daemon.py (new): the protocol client

```python
class DaemonError(Exception):      # .code (int; 0 when there is no daemon or the connection broke), .message
def socket_path(home: str) -> str  # <home>/run/soa-d.sock
def connect(home: str, *, autostart: bool = True, timeout: float = 10.0) -> Client
class Client:
    hello: dict                                                   # the hello result
    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict
    def on_event(self, fn) -> None   # fn(params) on the reader thread, for every event
    def close(self) -> None
```

- `connect` says hello (protocol 1, client `"tui"`). With `autostart`, when
  the socket does not answer it runs `soa-d serve --home <home>` (the binary
  is `$SOA_D_BIN`, else `soa-d` on PATH), detached (`start_new_session`,
  stdin `/dev/null`, stdout and stderr appended to `<home>/logs/soa-d.log`,
  the logs dir created if missing), and waits up to `timeout` for the socket.
  Two clients racing to start it is fine: the loser's `serve` exits with
  "already running". Without `autostart`, or when it times out:
  `DaemonError(code=0)`.
- `call` is thread-safe; an error response raises `DaemonError(code,
  message)`; a broken connection fails pending calls with code 0 and sends
  `{"kind": "connection.lost"}` to the event callbacks.

## son_of_anton_tui/backend.py: `DaemonBackend` (replaces `TextualBackend`)

```python
class DaemonBackend:
    def __init__(self, home: str, cwd: str, *, resume: str | None = None,
                 continue_last: bool = False, model: str | None = None, yolo: bool = False): ...
    session_id: str | None
    def start(self) -> bool    # connect, pick or create the session, attach as driver
    def finish(self) -> None   # detach and close; never stops the daemon or a running turn
```

`start`: `resume` attaches that id; `continue_last` the newest interactive
session (`session.list`); otherwise `session.create` in `cwd`. Then
`session.set` for `model` and for `yolo` (mode `yolo`). On failure it prints
the reason to stderr and returns False.

It implements what `tui.py` uses from its backend. Where `tui.py` reaches
into the Python agent (`backend.agent.redirect`, the `_approval_state` /
`_clarify_state` / `_model_picker_state` dicts with response queues, the
relay methods), change `tui.py` to call `DaemonBackend` methods instead. Keep
`tui.py`'s widgets, layout, keybinds, rendering and modal classes.

### Turns are driven by events

- Every message typed goes straight to `turn.send`; the daemon starts it or
  queues it. `app._busy` is `"turn"` exactly while the attached session's
  status is `running` and `None` otherwise, including turns the daemon starts
  itself (queued messages, background notices).
- `app._queued_items` mirrors the daemon queue's texts (`queue.updated`); the
  queue above the prompt and "N queued" render from it as today. `/cancel`:
  the existing picker over the daemon queue, then `queue.remove`. `/sendnow
  text`: `turn.send` with mode `now` (a plain send when idle). Bare
  `/sendnow`: `queue.steer`. `/sendall`: `queue.steer` with `all`.
- A user block is rendered when its `turn.start` arrives. A turn whose text
  starts with `[Background process` renders as a dim note, not a user block.
- Events onto the feed's existing kinds: `reasoning.delta` opens and feeds a
  reasoning block; `assistant.delta` a markdown block (each closes when a tool
  starts, a row is committed, or the turn ends); `tool.start` is `tool_start`
  with the `summary` as label; `tool.end` is `tool_done` with `duration_ms /
  1000` and `is_error`; rows pair by `call_id`; `turn.end` is `turn_end`, plus
  a dim note for `interrupted` and for `error` (with the error text);
  `notice` is a note. A live turn's `message` events render nothing (the
  deltas did) but advance the last seen seq.
- Ctrl+C (and whatever key interrupts today) while busy: `turn.interrupt`.
- History: after `start` and after `/resume`, the attach result's messages
  render like today's resume history: user rows as user blocks, assistant
  rows as markdown with reasoning collapsed, tool calls and results as
  finished tool rows, `[response interrupted]` / `[response failed]` rows as
  dim notes. If the session is running, the live tail that follows renders as
  live events.
- Connection lost (the daemon restarted): a note, then reconnect (with
  autostart) with backoff and re-attach with `after_seq` set to the last seq
  seen; on `gap`, clear the feed and render the returned page.

### Modals

- `approval.request`: the existing approval `ChoiceModal`, its detail the
  subject and reasons, its choices the event's (`once`, `session`, `always`,
  `deny` labelled as in `tui.py:1826-1829` today); Esc is `deny`; the answer
  goes to `approval.answer`. `approval.resolved` closes an open modal for that
  id (answered elsewhere, or the turn was interrupted).
- `question.request`: the existing clarify modal (choices, multi-select, or
  free text when there are no choices); the answer goes to `question.answer`;
  dismissing sends "The user dismissed the question without answering; use
  your best judgement." `question.resolved` closes it.
- No deadlines: the daemon waits.

### Status, sidebar, mode

`status_snapshot()` from the session object: `model_name` and `model_short`
(model), `session_title` (title, else preview), `reasoning_label`
(reasoning_effort, else "default"), `context_tokens`, `context_length` and
`context_percent` None (unknown until phase 3), `session_input_tokens`,
`session_output_tokens`, `session_api_calls` (usage), `active_background_processes`
(background_processes), `duration` (since created), `prompt_elapsed` (since
turn_started), `compactions` 0. `permission_mode()` is the session's mode;
`cycle_permission_mode()` (shift+tab) sets the next mode in the existing
order (`PERMISSION_MODES`) with `session.set`.

### Slash commands

All handled by the client; none reaches the Python CLI.

| command | does |
|---|---|
| `/model [name]` | `session.set` model; bare: one picker over `model.list` (no provider stage) |
| `/reasoning [level]` | `session.set` reasoning_effort; bare: a picker over the model's efforts |
| `/perm [mode]` | `session.set` mode; bare: a note with the mode |
| `/new`, `/clear` | `session.create` in the TUI's cwd, attach as driver, clear the feed |
| `/resume [id]`, `/sessions` | attach that id; bare: the existing filterable picker over `session.list` |
| `/commit [args]` | `command.run commit` |
| `/stop` | `command.run stop`; a note with the count |
| `/sendnow`, `/sendall`, `/cancel` | above |
| `/status` | a note: id, model, mode, tokens |
| `/help` | the supported commands |
| `/skin`, `/quit`, `/exit`, `:q`, Ctrl+L | local, unchanged |

Any other command in the registry: a note "/<name> is not available in soa-d
yet", and no turn. `!cmd` still runs locally in the session's cwd, its output
as notes. `palette_entries()` lists only the supported commands.

## The entry point (`son_of_anton_cli/main.py`)

- Interactive (`son-of-anton`, or `chat` without `-q`, `-z`, `-Q` or
  `--query-file`): `DaemonBackend(home=get_son_of_anton_home(), cwd=<--in or
  the cwd>, resume=…, continue_last=…, model=-m, yolo=--yolo)`, `start()`,
  then `run_app`. `-c` / `--continue` and `--resume latest` mean
  `continue_last`. A `--resume` id soa-d does not know: "session <id> is not
  in soa-d (Python sessions are imported in phase 8)", exit 1. Flags with no
  soa-d meaning (`--pickup`, `--resume @<tool>`, `-w`, `-t`, images,
  `--ignore-rules`, …): an error naming the flag, exit 2.
- `-z PROMPT`: exec `soa-d oneshot --home H [--model M] [--reasoning R]
  [--yolo] [--usage-file P] PROMPT`. `-q PROMPT` or `--query-file F`, without
  `-Q` and without `--resume`: exec `soa-d query --home H [--model M]
  [--reasoning R] [--yolo] PROMPT`. `--provider` is accepted when it names the
  configured provider. Any other flag with `-q` / `-z`: an error, exit 2.
- `chat -Q …` and every subcommand stay on the Python code, unchanged.

## Deletions

- `TextualBackend` (`son_of_anton_tui/backend.py`) and
  `son_of_anton_cli/relay_host.py` (only the TUI used it); `cli.py`'s `main()`
  no longer builds a `TextualBackend`; `SonOfAntonCLI` stays for `chat -Q`.
- Tests of the deleted code: `tests/test_tui_handoff.py`,
  `tests/test_tui_takeover.py`, and the tests in `tests/test_tui.py` that test
  `TextualBackend` itself (its streaming seams, output funnels, prompt queues
  and nudges, the two-stage model picker, hooks, the commit-identity tests).
  Tests that use the `backend` fixture only as a host for app-local behavior
  (editor compose, transcript resize, `:q` mid-turn, the sidebar's background
  row, the status row) move to a `DaemonBackend` fixture, assertions unchanged
  where the behavior still exists. List every deleted test in the commit body.
- Do not touch the gateway, `gateway/relay_workers.py`,
  `son_of_anton_cli/relay_worker.py`, or cron.

## Nix

- The `son-of-anton` package's runtime PATH includes `soa-d`
  (`nix/son-of-anton.nix` runtime deps, the package passed in from
  `nix/packages.nix`).
- The dev shell includes `soa-d`, so the tests find it.

## Not in 2b

Expanding `@path` references (the Python agent did it; the daemon sends the
text as typed), image attachments, `/compact`, titles, memory and recall
(phase 3), subagent views (phase 4), `/handoff` and `/pickup` (phase 5), MCP
and web tools (phase 7), Python sessions (the phase 8 importer).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the
whole suite), and `nix flake check`. One commit: summary `tui: phase 2b
(Textual TUI on soa-d, -q and -z on soa-d)`, body listing what changed and
every deleted test.
