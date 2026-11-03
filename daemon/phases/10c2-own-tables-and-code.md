# Phase 10c2: our own tables and code

Goal: as in 10c1 (read its "Goal" and "How to write it"): replace what soa-d
still carries from hermes-agent v0.20.5 (this repository's first commit,
`677b5c39`) and opencode v1.19.5. This phase does the tables and the code;
10c1 (words) and 10c3 (Nix) run at the same time, so stay inside the files
listed here.

The rule of 10c1 holds for code and data too: **write from behaviour, not
from the old version.** Work out what the code must do from its callers and
its tests, then write your own design. A line-by-line translation or a
reordered copy of a table is still the original; the reviewer compares the
result with hermes-agent and opencode.

## 1. Dangerous-command detection (`internal/approval/patterns.go`)

hermes's pattern table, ported. Replace it with a detector of your own
design: your own categories, structure and expressions (shell parsing where
it serves better than a regex is fine; no new dependency).

The contract is behaviour, and it is strict, because this is a safety
boundary:
- `internal/approval/testdata/python_verdicts.jsonl` (1022 synthetic commands
  with their recorded `dangerous` and `hardline` verdicts) and every existing
  test in `internal/approval/` pass unchanged.
- A row may change verdict only toward caution (safe to dangerous, dangerous
  to hardline), and only when you can say why the old verdict was wrong; mark
  it `"intentional"` with the reason. A row may never become less cautious.
- Add rows for anything your design catches that the corpus does not cover,
  with a reason.
- `testdata/README.md` says what the corpus is now: the recorded behaviour
  that any detector must keep.

## 2. The execute_code helper module (`internal/tools/code_stub.go`)

The Python module a script imports to call tools, generated from hermes's.
Write your own generator and module. Keep the interface the model is told
about (10c1 rewrites `execute_code`'s description, which names them): the
function names and their parameters, what each returns, and how a call
reaches the daemon. The `execute_code` tests pass unchanged.

## 3. The skins (`internal/tui/theme.go`)

The nine built-ins are hermes's (names, descriptions and colours). Replace
them with these, every key given explicitly:

| name | description | ui_accent | ui_thinking | ui_tool | ui_ok | ui_warn | ui_error |
|---|---|---|---|---|---|---|---|
| default | Your terminal's own colours | yellow | cyan | yellow | green | yellow | red |
| paper | For light terminals | #1F4E8C | #5B5FC7 | #1F4E8C | #2E7D32 | #9A6700 | #B3261E |
| ash | Greyscale | #C8C8C8 | #9E9E9E | #B0B0B0 | #A0A0A0 | #D0D0D0 | #EDEDED |
| tide | Cool blues | #6CB6D9 | #9C8CD9 | #6CB6D9 | #7CC4A0 | #D9B26C | #E07A6C |
| ember | Warm oranges | #E08A3C | #C9A0DC | #E08A3C | #8FBF6A | #E0B43C | #E05A4E |
| moss | Greens | #8DBF5A | #7FB2C9 | #8DBF5A | #8DBF5A | #D9B45A | #D9655A |

A `display.skin` naming a skin that no longer exists falls back to
`default`, as an unknown name does today (check that it does, and that the
TUI says so once).

## 4. What the TUI took from opencode's code

- `internal/tui/palette.go`: the greys and the muted text colour derived from
  the terminal background follow opencode's `generateGrayScale` and
  `generateMutedTextColor`. Design your own derivation: what has to hold is
  what the look tests check (surfaces stepping away from the background
  toward the foreground, readable muted text, the right polarity on light and
  dark terminals). Where a look test pins an exact colour, turn it into the
  property it stands for and list it in the report.
- `internal/tui/subagent.go` `childFooter` (opencode's SubagentFooter): keep
  what SPEC section 9 says the footer shows; your own layout code.
- `internal/tui/state.go` `titlecase` and `subagentDuration`, and
  `internal/server/summary.go` `titlecase`: your own (one shared helper is
  fine if both packages can use it).
- Comments in these files that cite opencode or the Python as the source say
  what the code does instead.

## Done when

The Go gates in `daemon/AGENTS.md` pass. One commit: summary `daemon: phase
10c2 (our own tables and code)`, body listing what changed, ending with the
`Model:` line. Report: each corpus row you added or marked intentional, and
each look test you changed, with the reason.
