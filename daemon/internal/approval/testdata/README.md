# python_verdicts.jsonl

The recorded behaviour that any dangerous-command detector must keep. The test
`TestThePortAgreesWithThePythonVerdicts` runs every row through
`approval.Classify`; `TestStricterRowsKeepTheirVerdict` holds the stricter rows
(below) to their recorded verdict.

One JSON object per line: `{"cmd", "dangerous", "hardline"}`, plus at most one of

- `"intentional"`: the reason soa-d's verdict differs from the first recording
  (see below). The main test skips these rows; a reason that starts with
  `stricter:` marks a row that is more cautious than the recording, and
  `TestStricterRowsKeepTheirVerdict` checks it.
- `"note"`: why the row exists, for a row added with the current detector. These
  rows are checked like the others.

## Where the rows came from

The first 1022 commands were synthetic, written to exercise the patterns of the
original (Python) classifier with positives, flag and quoting variants, and
near-miss negatives. They were never taken from a real shell history. Their
verdicts were recorded by running that classifier: `dangerous` was
`detect_dangerous_command(cmd)[0]` or hardline, `hardline` was
`detect_hardline_command(cmd)[0]` or `_check_sudo_stdin_guard(cmd)[0]`. The
Python and its generator script were deleted in phase 10b2 (they are in git
history before that commit).

Rows that mention `config.toml` take their verdict from the same command spelled
`config.yaml`, because the Python patterns named `config.yaml` where soa-d's
name the real `config.toml`. Rows that spell `config.yaml` are kept and marked
intentional. The rows for script interpreters (`python3 -c`, `bash -c 'ls'`,
heredocs to python and perl) and the two unterminated-quote rows are marked
intentional too: the Python asked about them and soa-d does not.

## Phase 10c2: the detector is soa-d's own

The pattern table was replaced by a tokenising detector (see `patterns.go`,
`scan.go`, `rules.go`). The rule for this file: **a row may change verdict only
toward caution** (safe to dangerous, dangerous to hardline), and only with a
reason. 33 rows did, each marked `"intentional": "stricter: ..."` with the
verdict the detector now gives; none became less cautious. The old Go detector
and the new one agree on every other row.

Beyond those, 50 rows were added with a `"note"`: the cases the new design
catches that the first corpus did not cover (options and wrappers looked
through, every operand of `rm`, path forms, quoted separators, project config
edits, extra spellings of the force options) and the near-misses that must stay
safe.

Add rows by hand. A row that should differ from what the detector gives needs
`"intentional"` and a reason.
