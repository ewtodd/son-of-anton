# python_verdicts.jsonl

Differential corpus for `TestThePortAgreesWithThePythonVerdicts`: one JSON
object per line, `{"cmd", "dangerous", "hardline"}`, plus `"intentional"` (the
reason) on the few rows where soa-d deliberately differs from the Python.

The commands were synthetic, written to exercise each pattern with positives,
flag and quoting variants, and near-miss negatives. They were never taken from a
real shell history. The verdicts were produced by running the reference
functions in the Python `tools/approval.py`: `dangerous` was
`detect_dangerous_command(cmd)[0]` or hardline, `hardline` was
`detect_hardline_command(cmd)[0]` or `_check_sudo_stdin_guard(cmd)[0]`.

Rows that mention `config.toml` take their verdict from the same command spelled
`config.yaml`, because the Python patterns name `config.yaml` where soa-d's name
the real `config.toml`. Rows that spell `config.yaml` are kept and marked
intentional.

The Python and its generator script were deleted in phase 10b2 (they are in
git history before that commit). The corpus is now a recorded fixture: add rows
by hand, with `"intentional"` saying why a row is Go's own verdict.
