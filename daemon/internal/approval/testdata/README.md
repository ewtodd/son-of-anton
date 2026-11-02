# python_verdicts.jsonl

Differential corpus for `TestThePortAgreesWithThePythonVerdicts`: one JSON
object per line, `{"cmd", "dangerous", "hardline"}`, plus `"intentional"` (the
reason) on the few rows where soa-d deliberately differs from the Python.

The commands are synthetic, written in `gen_python_verdicts.py` to exercise each
pattern with positives, flag and quoting variants, and near-miss negatives.
They are never taken from a real shell history. The verdicts are produced by
running the reference functions in `tools/approval.py`: `dangerous` is
`detect_dangerous_command(cmd)[0]` or hardline, `hardline` is
`detect_hardline_command(cmd)[0]` or `_check_sudo_stdin_guard(cmd)[0]`.

Rows that mention `config.toml` take their verdict from the same command spelled
`config.yaml`, because the Python patterns name `config.yaml` where soa-d's name
the real `config.toml`. Rows that spell `config.yaml` are kept and marked
intentional.

Regenerate from the repo root (needs the Nix dev shell for the Python):

    nix develop -c sh -c '"$SON_OF_ANTON_PYTHON" daemon/internal/approval/testdata/gen_python_verdicts.py'

The generator puts the repo root on `sys.path` and points `HOME` and
`SON_OF_ANTON_HOME` at temporary directories. Add rows to the generator, never
by hand to the `.jsonl`.
