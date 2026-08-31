"""Generate a physics-mode ``problem.yaml`` from a dataset and a one-line goal.

The physics modes score a run by reading ``RESULTS.txt`` from its workspace and
comparing the values against the ``checks`` in a problem spec. Writing that
spec by hand means listing every key, every expected value and every tolerance,
and keeping the prose in sync with them — which is why the three specs in
``problems/`` are all toy problems on synthetic data.

This script takes the human input down to two things — where the data is, and
what you want out of it — and keeps the LLM's share just as small. The split:

* **The probe does the mechanical work, deterministically.** It walks the data
  directory and reports what is actually there: ROOT trees and their branches
  and entry counts (via ``uproot``, no ROOT build needed), CSV headers and a
  couple of rows, ``.npy`` shapes and dtypes, sizes, top-level definitions in
  any ``.py`` alongside the data. The probe runs under the *physics
  interpreter*, inside the *same bubblewrap sandbox* a run's computations use,
  with the data mounted read-only — so what it can see is exactly what the
  agent will be able to see.
* **The LLM writes only prose and thresholds.** It never sees the data, only
  the probe's summary and your goal, and it returns strict JSON: a name, the
  task statement, and the ``checks``. With ``--truth`` it does not even pick
  the expected values — those come from a reference ``RESULTS.txt`` you already
  trust, and the LLM only writes the task around them.
* **The validator refuses bad output.** Slug, non-empty task, every check
  keyed and numeric, every check key named in the task text (otherwise the
  agent is graded on a key it was never asked to produce), tolerances present
  and positive. One repair round-trip, then it gives up rather than writing a
  spec that cannot be satisfied.

``--no-llm`` skips the model entirely and renders the spec from the probe plus
a ``--truth`` file, for when you want the whole thing deterministic.

Usage::

    son-of-anton problem create \\
        --data ~/UM-ANSG/YAP-PSD/root_files \\
        --goal "pulse-shape discrimination should work with machine learning" \\
        --truth reference_results.txt \\
        -o problems/yap_psd_real/problem.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from pathlib import Path

import yaml

from physics_intern.utils.sandbox import (
    SandboxPolicy,
    execute_python,
    runtime_summary,
)

MAX_PROBE_BYTES = 60_000
DEFAULT_TOLERANCE_FRAC = 0.10


# ---------------------------------------------------------------------------
# Probe — deterministic, runs sandboxed under the physics interpreter
# ---------------------------------------------------------------------------

PROBE_SCRIPT = r'''
"""Summarize a data directory. Emits JSON on stdout. Reads nothing it cannot
open read-only, and never loads a whole array into memory."""
import json, os, sys
from pathlib import Path

roots = [Path(p) for p in sys.argv[1:]]
MAX_FILES = 400
report = {"roots": [str(r) for r in roots], "entries": [], "truncated": False}


def describe_root_file(path):
    try:
        import uproot
    except ImportError:
        return {"note": "uproot unavailable — install it in the physics runtime"}
    out = {"trees": []}
    try:
        with uproot.open(str(path)) as handle:
            for key, obj in handle.items(recursive=False):
                classname = getattr(obj, "classname", "")
                if "TTree" not in str(classname):
                    out.setdefault("objects", []).append(
                        {"name": key, "class": str(classname)}
                    )
                    continue
                branches = []
                for bname, branch in obj.items():
                    branches.append(
                        {
                            "name": bname,
                            "type": str(getattr(branch, "typename", "")),
                        }
                    )
                out["trees"].append(
                    {
                        "name": key.split(";")[0],
                        "entries": int(obj.num_entries),
                        "branches": branches[:60],
                        "branch_count": len(branches),
                    }
                )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def describe_npy(path):
    try:
        import numpy as np
        arr = np.load(str(path), mmap_mode="r", allow_pickle=False)
        return {"shape": list(arr.shape), "dtype": str(arr.dtype)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def describe_csv(path):
    try:
        with open(path, "r", errors="replace") as handle:
            lines = [next(handle, "").rstrip("\n") for _ in range(3)]
        return {"header": lines[0][:400], "sample_rows": [l[:400] for l in lines[1:] if l]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def describe_text(path, limit=2000):
    try:
        return {"head": path.read_text(errors="replace")[:limit]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def describe_py(path):
    try:
        import ast
        tree = ast.parse(path.read_text(errors="replace"))
        names = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        doc = ast.get_docstring(tree) or ""
        return {"definitions": names[:40], "docstring": doc[:600]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


count = 0
for root in roots:
    if not root.exists():
        report["entries"].append({"path": str(root), "error": "does not exist"})
        continue
    paths = sorted(root.rglob("*")) if root.is_dir() else [root]
    for path in paths:
        if not path.is_file():
            continue
        count += 1
        if count > MAX_FILES:
            report["truncated"] = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry = {"path": str(path), "size_bytes": size, "suffix": path.suffix}
        suffix = path.suffix.lower()
        if suffix == ".root":
            entry["root"] = describe_root_file(path)
        elif suffix == ".npy":
            entry["npy"] = describe_npy(path)
        elif suffix in (".csv", ".tsv", ".stats"):
            entry["csv"] = describe_csv(path)
        elif suffix in (".md", ".txt", ".rst"):
            entry["text"] = describe_text(path)
        elif suffix == ".py":
            entry["py"] = describe_py(path)
        report["entries"].append(entry)

print(json.dumps(report))
'''


def probe_data(
    data_paths: list[Path], policy: SandboxPolicy, timeout: int = 900
) -> dict:
    """Run the probe under the sandbox with *data_paths* mounted read-only."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="soa-probe-") as tmp:
        workdir = Path(tmp)
        script = workdir / "probe.py"
        # argv is not available through execute_python, so the paths are baked
        # into the script rather than passed — the probe reads sys.argv, and
        # this keeps the sandbox interface to "run this file".
        script.write_text(
            PROBE_SCRIPT.replace(
                "roots = [Path(p) for p in sys.argv[1:]]",
                f"roots = [Path(p) for p in {[str(p) for p in data_paths]!r}]",
            )
        )
        probe_policy = SandboxPolicy(
            interpreter=policy.interpreter,
            workspace=workdir,
            data_dirs=policy.data_dirs,
            mode=policy.mode,
            network=False,
            file_size_limit_mb=policy.file_size_limit_mb,
        )
        result = execute_python(script, timeout=timeout, cwd=workdir, policy=probe_policy)
    if result.returncode != 0:
        raise SystemExit(
            f"probe failed (rc={result.returncode}):\n{result.stderr.strip()[:4000]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"probe produced unparseable output: {exc}") from exc


def render_data_card(report: dict, limit: int = MAX_PROBE_BYTES) -> str:
    """Render the probe report as compact text for the model."""
    lines: list[str] = []
    for entry in report.get("entries", []):
        path = entry.get("path", "")
        size = entry.get("size_bytes", 0)
        lines.append(f"- {path} ({_human_size(size)})")
        if "error" in entry:
            lines.append(f"    error: {entry['error']}")
        root = entry.get("root") or {}
        for tree in root.get("trees", []):
            branch_names = ", ".join(
                f"{b['name']}:{b['type']}" for b in tree.get("branches", [])
            )
            lines.append(
                f"    TTree {tree['name']}: {tree['entries']:,} entries, "
                f"{tree['branch_count']} branches"
            )
            if branch_names:
                lines.append(f"      branches: {branch_names}")
        for obj in root.get("objects", []) or []:
            lines.append(f"    object {obj['name']} ({obj['class']})")
        if root.get("error"):
            lines.append(f"    root read error: {root['error']}")
        if root.get("note"):
            lines.append(f"    {root['note']}")
        npy = entry.get("npy") or {}
        if npy.get("shape") is not None:
            lines.append(f"    ndarray shape={npy['shape']} dtype={npy.get('dtype')}")
        csv = entry.get("csv") or {}
        if csv.get("header"):
            lines.append(f"    header: {csv['header']}")
            for row in csv.get("sample_rows", []):
                lines.append(f"    row:    {row}")
        py = entry.get("py") or {}
        if py.get("definitions"):
            lines.append(f"    defines: {', '.join(py['definitions'])}")
        if py.get("docstring"):
            first = py["docstring"].strip().splitlines()[0] if py["docstring"].strip() else ""
            if first:
                lines.append(f"    docstring: {first}")
        text = entry.get("text") or {}
        if text.get("head"):
            snippet = " ".join(text["head"].split())[:300]
            lines.append(f"    text: {snippet}")
    if report.get("truncated"):
        lines.append("- (listing truncated — more files present)")
    card = "\n".join(lines)
    if len(card) > limit:
        card = card[:limit] + "\n- (data card truncated)"
    return card


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


def load_truth(path: Path, tolerance_frac: float) -> list[dict]:
    """Turn a reference ``key = value`` file into ``checks`` entries.

    Tolerance is a fraction of the reference magnitude, floored so a reference
    value of exactly zero still gets a usable window.
    """
    checks: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # "key = value  # tolerance: 0.02" overrides the fractional default.
        explicit = None
        match = re.search(r"#\s*tolerance:\s*([0-9.eE+-]+)", value)
        if match:
            explicit = float(match.group(1))
        value = value.split("#")[0].strip()
        try:
            expected = float(value)
        except ValueError:
            continue
        tolerance = (
            explicit
            if explicit is not None
            else max(abs(expected) * tolerance_frac, 1e-6)
        )
        checks.append(
            {
                "id": key,
                "key": key,
                "expected": expected,
                "tolerance": float(f"{tolerance:.6g}"),
            }
        )
    if not checks:
        raise SystemExit(f"no numeric 'key = value' lines found in {path}")
    return checks


# ---------------------------------------------------------------------------
# LLM pass
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write problem specifications for an autonomous physics research agent.

The agent is given only the task statement you write. It runs Python in a
sandbox with the listed data mounted read-only, and it is scored by writing a
RESULTS.txt of `key = value` lines into its workspace, which is then compared
against numeric checks.

Return STRICT JSON, no prose, no markdown fence, with exactly these keys:

{
  "name": "snake_case_identifier",
  "problem": "the full task statement given to the agent",
  "checks": [{"id": "...", "key": "...", "expected": 0.0, "tolerance": 0.0}]
}

Rules for "problem":
- State the physics goal, the data (by absolute path, as listed), and the
  method latitude the user allowed. Do not invent data that was not listed.
- Say explicitly which `key = value` lines must be written to RESULTS.txt.
  Every key in "checks" must appear by name in the task statement.
- Do not prescribe the analysis in detail unless the user's goal did. The
  agent is meant to choose the method.
- Do not state the expected numeric values. The agent must not be told the
  answers it is being scored against.

Rules for "checks":
- One per scored quantity. "expected" and "tolerance" are numbers, never
  strings or nulls. "tolerance" is an absolute window and must be > 0.
- Choose tolerances a competent independent reproduction would land inside.
"""


def call_model(system: str, user: str, model: str | None) -> str:
    """One chat completion through the physics endpoint resolution."""
    from physics_intern.core.config import build_config
    from physics_intern.llm import call_llm

    config = build_config(None)
    if model:
        config.model = model
    elif not config.model:
        try:
            from son_of_anton_cli.config import load_config

            config.model = str(
                ((load_config() or {}).get("physics") or {}).get("model") or ""
            )
        except Exception:
            pass
    response = call_llm(system, user, config, agent_name="spec_writer")
    return response.text


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from *text*, fenced or bare."""
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(stripped[start : end + 1])


def validate_spec(spec: dict, require_checks: bool = True) -> list[str]:
    """Return a list of problems with *spec*. Empty means it is usable."""
    problems: list[str] = []
    name = str(spec.get("name") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{1,60}", name):
        problems.append(
            f"'name' must be a lowercase snake_case slug (got {name!r})"
        )
    text = str(spec.get("problem") or "").strip()
    if len(text) < 80:
        problems.append("'problem' is missing or too short to brief an agent")
    checks = spec.get("checks") or []
    if require_checks and not checks:
        problems.append("'checks' is empty — the run would have nothing to score")
    seen_keys: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            problems.append(f"checks[{index}] is not an object")
            continue
        key = str(check.get("key") or "")
        if not key:
            problems.append(f"checks[{index}] has no 'key'")
            continue
        if key in seen_keys:
            problems.append(f"checks[{index}] repeats key {key!r}")
        seen_keys.add(key)
        if not isinstance(check.get("expected"), (int, float)):
            problems.append(f"checks[{index}] ({key}) 'expected' is not a number")
        tolerance = check.get("tolerance")
        if not isinstance(tolerance, (int, float)) or tolerance <= 0:
            problems.append(
                f"checks[{index}] ({key}) needs a positive numeric 'tolerance'"
            )
        if key not in text:
            problems.append(
                f"checks[{index}] scores {key!r}, but the task statement never "
                f"asks the agent to write it to RESULTS.txt"
            )
    return problems


def build_user_message(
    goal: str,
    data_paths: list[Path],
    data_card: str,
    truth_checks: list[dict] | None,
    runtime: dict,
) -> str:
    packages = ", ".join(
        f"{k} {v}" for k, v in (runtime.get("packages") or {}).items()
    ) or "standard library only"
    parts = [
        f"## Goal (from the user, verbatim)\n{goal}",
        "\n## Data mounted read-only at these paths\n"
        + "\n".join(f"- {p}" for p in data_paths),
        f"\n## What the probe found\n{data_card}",
        f"\n## Packages available to the agent\n{packages}",
    ]
    if truth_checks is not None:
        parts.append(
            "\n## Checks (FIXED — copy these into \"checks\" verbatim)\n"
            + json.dumps(truth_checks, indent=2)
            + "\nThese come from a trusted reference run. Do not change the "
            "expected values or tolerances, and do not add or remove checks. "
            "Your job is to write the task statement around them, naming every "
            "one of these keys as a RESULTS.txt line the agent must produce."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_spec(
    spec: dict, data_paths: list[Path], goal: str, source_note: str
) -> str:
    """Render the final problem.yaml text."""
    document = {
        "name": spec["name"],
        "data": [str(p) for p in data_paths],
        "problem": spec["problem"].strip() + "\n",
        "checks": spec["checks"],
    }
    header = textwrap.dedent(
        f"""\
        # Generated by `son-of-anton problem create`
        # Goal: {goal}
        # {source_note}
        #
        # `data:` paths are bind-mounted read-only into every computation this
        # run performs. Nothing else outside the run's workspace is visible.
        """
    )
    body = yaml.dump(
        document, default_flow_style=False, sort_keys=False, allow_unicode=True, width=88
    )
    return header + body


def deterministic_spec(
    name: str, goal: str, data_paths: list[Path], checks: list[dict], data_card: str
) -> dict:
    """Render a spec with no model in the loop, from the probe and --truth."""
    keys = "\n".join(f"  {c['key']} = <value>" for c in checks)
    listing = "\n".join(f"  {p}" for p in data_paths)
    problem = textwrap.dedent(
        f"""\
        {goal.strip()}

        The data is mounted read-only at:
        {listing}

        What the probe found in it:
        {textwrap.indent(data_card, "  ")}

        Choose your own method. When you have a result, write RESULTS.txt in
        your workspace with one `key = value` line for each of:

        {keys}

        Write the final values only — the file is read verbatim and scored.
        """
    )
    return {"name": name, "problem": problem, "checks": checks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach the spec-builder options to *parser*.

    Shared so the CLI subcommand and any direct ``python -m`` invocation take
    exactly the same flags, rather than drifting into two interfaces.
    """
    parser.add_argument(
        "--data",
        action="append",
        required=True,
        metavar="PATH",
        help="Data file or directory to expose read-only. Repeatable.",
    )
    parser.add_argument(
        "--goal",
        required=True,
        help="One line: what you want out of the data, and how much method "
        'latitude the agent has (e.g. "PSD should work with machine learning").',
    )
    parser.add_argument(
        "-o", "--out", required=True, type=Path, help="Where to write problem.yaml"
    )
    parser.add_argument(
        "--truth",
        type=Path,
        help="Reference RESULTS.txt whose values become the expected values. "
        "Without it the model proposes both the keys and the thresholds.",
    )
    parser.add_argument(
        "--tolerance-frac",
        type=float,
        default=DEFAULT_TOLERANCE_FRAC,
        help="Tolerance as a fraction of each reference value "
        f"(default {DEFAULT_TOLERANCE_FRAC}). Per-key override: append "
        "'# tolerance: 0.02' to a line in the truth file.",
    )
    parser.add_argument("--name", help="Spec name. Default: derived by the model.")
    parser.add_argument("--model", help="Override the model used for the one LLM call.")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the model entirely and render from the probe plus --truth.",
    )
    parser.add_argument(
        "--probe-timeout", type=int, default=900, help="Seconds for the probe."
    )
    parser.add_argument(
        "--print-card",
        action="store_true",
        help="Print the data card and exit without writing a spec.",
    )
    return parser


def run(args, parser: argparse.ArgumentParser | None = None) -> int:
    """Build one problem spec from parsed *args*. Returns an exit code."""
    parser = parser or argparse.ArgumentParser(prog="son-of-anton problem create")

    data_paths = []
    for raw in args.data:
        path = Path(os.path.expanduser(raw)).resolve()
        if not path.exists():
            parser.error(f"--data path does not exist: {path}")
        data_paths.append(path)

    if args.no_llm and not args.truth:
        parser.error("--no-llm needs --truth: without a model there is nothing to "
                     "derive the checks from.")
    if args.no_llm and not args.name:
        parser.error("--no-llm needs --name.")

    policy = SandboxPolicy.from_config(extra_data_dirs=data_paths)
    runtime = runtime_summary(policy.interpreter)
    print(f"runtime:  {policy.interpreter}", file=sys.stderr)
    print(
        f"sandbox:  {'bubblewrap' if policy.mode != 'off' else 'OFF (unconfined)'}",
        file=sys.stderr,
    )
    if not runtime.get("packages"):
        print(
            "warning:  the physics interpreter has no scientific packages; the "
            "probe cannot read ROOT or .npy files and the agent will not be "
            "able to analyse them either. Set physics.python in config.yaml.",
            file=sys.stderr,
        )

    print("probing data...", file=sys.stderr)
    report = probe_data(data_paths, policy, timeout=args.probe_timeout)
    data_card = render_data_card(report)
    if args.print_card:
        print(data_card)
        return 0

    truth_checks = load_truth(args.truth, args.tolerance_frac) if args.truth else None

    if args.no_llm:
        spec = deterministic_spec(
            args.name, args.goal, data_paths, truth_checks, data_card
        )
        source_note = f"Checks from {args.truth} (no model in the loop)."
    else:
        user_message = build_user_message(
            args.goal, data_paths, data_card, truth_checks, runtime
        )
        system = SYSTEM_PROMPT
        if args.name:
            system += f'\nUse exactly this name: "{args.name}".\n'
        print("asking the model for the task statement...", file=sys.stderr)
        raw = call_model(system, user_message, args.model)
        try:
            spec = parse_json_object(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            spec = {}
            issues = [f"output was not valid JSON: {exc}"]
        else:
            if truth_checks is not None:
                # The checks are not the model's to change.
                spec["checks"] = truth_checks
            issues = validate_spec(spec)

        if issues:
            print(
                "spec rejected, asking once for a repair:\n  "
                + "\n  ".join(issues),
                file=sys.stderr,
            )
            repair = (
                user_message
                + "\n\n## Your previous output was rejected\n"
                + "\n".join(f"- {issue}" for issue in issues)
                + "\n\nReturn the corrected JSON object. Nothing else."
            )
            raw = call_model(system, repair, args.model)
            spec = parse_json_object(raw)
            if truth_checks is not None:
                spec["checks"] = truth_checks
            issues = validate_spec(spec)
            if issues:
                print("still invalid:\n  " + "\n  ".join(issues), file=sys.stderr)
                return 1
        if args.name:
            spec["name"] = args.name
        source_note = (
            f"Checks fixed from {args.truth}; task statement written by the model."
            if truth_checks is not None
            else "Task statement and checks written by the model from the probe."
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_spec(spec, data_paths, args.goal, source_note))
    print(f"\nwrote {args.out}", file=sys.stderr)
    print(f"  name:   {spec['name']}", file=sys.stderr)
    print(f"  checks: {len(spec['checks'])}", file=sys.stderr)
    for check in spec["checks"]:
        print(
            f"    {check['key']} = {check['expected']} ± {check['tolerance']}",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: ``python -m physics_intern.spec_builder``."""
    parser = add_arguments(
        argparse.ArgumentParser(
            prog="son-of-anton problem create",
            description="Generate a physics-mode problem.yaml from data plus a goal.",
        )
    )
    return run(parser.parse_args(argv), parser)


if __name__ == "__main__":
    raise SystemExit(main())
