"""Experimental answer checking for the physics modes.

Replaces physics-intern's symbolic (SymPy) theory checker. An experimental
problem ships a spec that lists numeric ``checks``; the agent's workspace must
produce a ``RESULTS.txt`` of ``key = value`` lines (from its ROOT macro or
Python analysis). Each check is compared against the expected value within
tolerance.

If the spec names a ``checker`` script, it runs first (the canonical place
for domain-specific validation) and may rewrite RESULTS.txt before the
numeric comparisons.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FormalEvalResult:
    """Result of the formal evaluation of one workspace."""

    passed: bool
    checks: list[dict] = field(default_factory=list)
    message: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for check in self.checks if check.get("passed"))

    @property
    def total_count(self) -> int:
        return len(self.checks)


def extract_answer_code(response_text: str) -> str | None:
    """Return the last python code block containing ``def answer`` in *response_text*."""
    pattern = r"```python\s*\n(.*?)```"
    blocks = re.findall(pattern, response_text, re.DOTALL)
    for block in reversed(blocks):
        if "def answer" in block:
            return block.strip()
    return None


def _load_results(workspace: Path) -> dict[str, str]:
    """Parse RESULTS.txt as key = value lines. Missing file -> {}."""
    results: dict[str, str] = {}
    path = workspace / "RESULTS.txt"
    if not path.exists():
        return results
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            results[key.strip()] = value.strip()
    return results


def _parse_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _run_checker_script(workspace: Path, problem_def: dict) -> None:
    """Run the spec's optional checker script inside the workspace."""
    checker = problem_def.get("checker")
    if not checker:
        return
    script = workspace / checker
    if not script.exists():
        return
    subprocess.run(
        ["python3", str(script)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )


def _check_one(spec: dict, results: dict[str, str]) -> dict:
    key = spec.get("key", "")
    expected = _parse_number(spec.get("expected"))
    tolerance = _parse_number(spec.get("tolerance"))
    tolerance = tolerance if tolerance is not None else 0.0
    raw = results.get(key)
    candidate = _parse_number(raw) if raw is not None else None

    check = {
        "id": spec.get("id", key),
        "key": key,
        "expected": expected,
        "candidate": candidate,
        "passed": False,
        "details": "",
    }
    if candidate is None:
        check["details"] = f"RESULTS.txt has no numeric value for {key!r}"
        return check
    if expected is None:
        check["details"] = f"check for {key!r} has no numeric expected value"
        return check
    if tolerance > 0:
        passed = abs(candidate - expected) <= tolerance
        check["details"] = (
            f"candidate={candidate}, expected={expected}, tolerance={tolerance}"
        )
    else:
        passed = abs(candidate - expected) <= 1e-9 * max(1.0, abs(expected))
        check["details"] = f"candidate={candidate}, expected={expected} (exact)"
    check["passed"] = passed
    return check


def run_formal_evaluation(
    workspace_path: str,
    problem_def: dict,
    problem_path: str | None = None,
) -> FormalEvalResult:
    """Evaluate a workspace against the problem spec's numeric checks.

    *problem_def* is the parsed problem.yaml. *problem_path* is retained for
    signature compatibility with the theory-era callers and ignored.
    """
    workspace = Path(workspace_path)
    _run_checker_script(workspace, problem_def)
    results = _load_results(workspace)

    specs = problem_def.get("checks") or []
    checks = [_check_one(spec, results) for spec in specs]
    passed = bool(checks) and all(check["passed"] for check in checks)

    message = ""
    if not checks:
        message = "no numeric checks in problem spec — nothing to evaluate"
    elif passed:
        message = f"all {len(checks)} checks passed"
    else:
        failed = [check["id"] for check in checks if not check["passed"]]
        message = f"failed checks: {', '.join(failed)}"

    return FormalEvalResult(passed=passed, checks=checks, message=message)


def render_formal_evaluation(result: FormalEvalResult) -> None:
    """Print the evaluation to the console."""
    from physics_intern.core.console import console

    if result.message:
        style = "green" if result.passed else "red"
        console.print(f"[{style}]{result.message}[/{style}]")
    for check in result.checks:
        mark = "[green]PASS[/green]" if check["passed"] else "[red]FAIL[/red]"
        console.print(f"  {mark} {check['id']}: {check['details']}")


def write_formal_eval_report(result: FormalEvalResult, workspace_path: str) -> None:
    """Write FORMAL_EVAL.md into the workspace."""
    lines = [
        "# Formal Evaluation",
        "",
        f"- Status: {'PASSED' if result.passed else 'FAILED'}",
        f"- Checks: {result.passed_count}/{result.total_count} passed",
        "",
    ]
    for check in result.checks:
        mark = "PASS" if check["passed"] else "FAIL"
        lines.append(
            f"- {mark} `{check['id']}` ({check['key']}): {check['details']}"
        )
    (Path(workspace_path) / "FORMAL_EVAL.md").write_text(
        "\n".join(lines) + "\n"
    )


def load_or_run_formal_eval(
    workspace_path: str,
    problem_def: dict,
    problem_path: str | None = None,
) -> FormalEvalResult | None:
    """Return a cached evaluation if FORMAL_EVAL.md exists, else None."""
    report = Path(workspace_path) / "FORMAL_EVAL.md"
    if not report.exists():
        return None
    text = report.read_text()
    if "Status: PASSED" in text:
        return FormalEvalResult(passed=True, checks=[], message="cached: PASSED")
    if "Status: FAILED" in text:
        return FormalEvalResult(passed=False, checks=[], message="cached: FAILED")
    return None
