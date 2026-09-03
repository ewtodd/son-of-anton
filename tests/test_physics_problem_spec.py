"""A run has to carry its problem spec into its workspace, or it is not scored.

The bug this covers: the run is scored by comparing the workspace's RESULTS.txt
against the numeric `checks` in a `problem.yaml` that the evaluator reads
*out of the workspace*. A run that never puts one there is silently never
scored — it finishes, prints an answer, and reports "Formal verification
skipped".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from physics_intern.core.problem_spec import ProblemSpec, load_spec, write_spec

REPO_ROOT = Path(__file__).resolve().parent.parent

SPEC = {
    "name": "decay_curve",
    "problem": "Measure the half-life. Write halflife_s to RESULTS.txt.",
    "answer_template": "def answer():\n    return {}\n",
    "checks": [
        {"id": "halflife", "key": "halflife_s", "expected": 119.2, "tolerance": 4.0}
    ],
}


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "problem.yaml"
    path.write_text(yaml.dump(SPEC), encoding="utf-8")
    return path


def test_a_spec_path_is_loaded(spec_file: Path) -> None:
    spec = load_spec(str(spec_file))
    assert spec.name == "decay_curve"
    assert spec.text.startswith("Measure the half-life")
    assert spec.answer_template.startswith("def answer")
    assert spec.is_scored


def test_a_tilde_path_is_expanded(spec_file: Path, monkeypatch) -> None:
    """`~/runs/problem.yaml` is what a person types at a chat prompt.

    Path does not expand it, so unexpanded the spec is not a file, the prose
    branch takes it, and the run's problem statement becomes the literal string
    "~/runs/problem.yaml" — an expensive run answering nothing, scored against
    nothing, with no error anywhere.
    """
    monkeypatch.setenv("HOME", str(spec_file.parent))
    spec = load_spec(f"~/{spec_file.name}")
    assert spec.definition is not None, "the tilde was not expanded"
    assert spec.is_scored


def test_a_path_that_looks_like_a_spec_but_is_not_warns(tmp_path) -> None:
    """Silence here means an unscored run nobody asked for."""
    with pytest.warns(UserWarning, match="not a readable file"):
        spec = load_spec(str(tmp_path / "typo.yaml"))
    assert spec.definition is None


def test_plain_prose_is_the_problem_itself() -> None:
    spec = load_spec("Why is the sky blue?")
    assert spec.text == "Why is the sky blue?"
    assert spec.definition is None
    assert spec.is_scored is False


def test_a_yaml_without_a_problem_key_is_not_a_spec(tmp_path: Path) -> None:
    path = tmp_path / "notaspec.yaml"
    path.write_text(yaml.dump({"unrelated": 1}), encoding="utf-8")
    assert load_spec(str(path)).definition is None


def test_unparseable_yaml_is_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("{{{ not yaml", encoding="utf-8")
    assert load_spec(str(path)).definition is None


def test_a_name_falls_back_to_the_filename(tmp_path: Path) -> None:
    path = tmp_path / "bromine.yaml"
    path.write_text(
        yaml.dump({"problem": "p", "checks": []}), encoding="utf-8"
    )
    assert load_spec(str(path)).name == "bromine"


def test_write_spec_puts_it_where_the_evaluator_looks(tmp_path: Path) -> None:
    assert write_spec(tmp_path, ProblemSpec(text="p", definition=SPEC)) is True
    written = yaml.safe_load((tmp_path / "problem.yaml").read_text())
    assert written["checks"][0]["key"] == "halflife_s"


def test_write_spec_is_a_no_op_without_a_spec(tmp_path: Path) -> None:
    assert write_spec(tmp_path, ProblemSpec(text="p")) is False
    assert not (tmp_path / "problem.yaml").exists()


# --- the entry points must actually pass it -------------------------------


def _call_kwargs(path: str, function: str, callee: str) -> list[set[str]]:
    """Keyword names each `callee(...)` call inside *function* is given."""
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = getattr(inner.func, "id", None) or getattr(
                inner.func, "attr", None
            )
            if name == callee:
                found.append({kw.arg for kw in inner.keywords if kw.arg})
    return found


@pytest.mark.parametrize(
    ("path", "function"),
    [
        ("cli.py", "_run_problem_mode"),
        ("gateway/run.py", "_run_physics_mode_sync"),
    ],
)
def test_every_entry_point_goes_through_one_runner(path: str, function: str) -> None:
    """Near-copies of the run logic is how the modes drifted apart before."""
    calls = _call_kwargs(path, function, "run_problem")
    assert calls, (
        f"{path}:{function} does not call physics_intern.run.run_problem — a "
        "second copy of the run logic is how the modes drifted apart before"
    )
    for kwargs in calls:
        assert "mode" in kwargs


def test_the_runner_passes_the_spec(monkeypatch, tmp_path) -> None:
    """The one place that has to get it right, now that it is the only place."""
    import physics_intern.run as run_module

    seen: dict = {}

    def fake_autophysicist(**kwargs):
        seen.update(kwargs)
        return tmp_path

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_autophysicist
    )

    spec_path = tmp_path / "problem.yaml"
    spec_path.write_text(yaml.dump(SPEC), encoding="utf-8")
    run_module.run_problem(str(spec_path), mode="physics")

    assert seen["problem_def"] == SPEC
    assert seen["problem_name"] == "decay_curve"
    assert seen["answer_template"].startswith("def answer")


def test_max_iterations_is_settable(monkeypatch) -> None:
    """Physics mode took a hardcoded 50 and has no wall-clock or cost gate."""
    from physics_intern.run import resolve_max_iterations

    monkeypatch.setattr("physics_intern.run._physics_config", dict)
    assert resolve_max_iterations(5, 50) == 5
    assert resolve_max_iterations(None, 50) == 50

    monkeypatch.setattr(
        "physics_intern.run._physics_config", lambda: {"max_iterations": 12}
    )
    assert resolve_max_iterations(None, 50) == 12
    assert resolve_max_iterations(3, 50) == 3, "the flag must win over config"


def test_an_unknown_mode_is_refused() -> None:
    from physics_intern.run import run_problem

    with pytest.raises(ValueError, match="unknown mode"):
        run_problem("x", mode="nonsense")
    with pytest.raises(ValueError, match="unknown mode"):
        run_problem("x", mode="research")


def test_script_timeout_is_settable(monkeypatch) -> None:
    """60s came from a symbolic-work scaffold; multi-GB reads do not fit in it."""
    from physics_intern.run import resolve_script_timeout

    monkeypatch.setattr("physics_intern.run._physics_config", dict)
    assert resolve_script_timeout(None) == 60
    assert resolve_script_timeout(900) == 900

    monkeypatch.setattr(
        "physics_intern.run._physics_config", lambda: {"script_timeout": 600}
    )
    assert resolve_script_timeout(None) == 600
    assert resolve_script_timeout(120) == 120, "the flag must win over config"


def test_the_runner_passes_the_script_timeout(monkeypatch, tmp_path) -> None:
    import physics_intern.run as run_module

    seen: dict = {}

    def fake_autophysicist(**kwargs):
        seen.update(kwargs)
        return tmp_path

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_autophysicist
    )
    monkeypatch.setattr("physics_intern.run._physics_config", dict)
    run_module.run_problem("a question", mode="physics", script_timeout=900)
    assert seen["sandbox_timeout"] == 900
