"""A run has to carry its problem spec into its workspace, or it is not scored.

The bug this covers: both modes are scored by comparing the workspace's
RESULTS.txt against the numeric `checks` in a `problem.yaml` that the evaluator
reads *out of the workspace*. Research mode never put one there — its entry
points passed the raw message text and nothing else — so
`_run_formal_verification` printed "skipped: no problem.yaml" on every run and
the checks never executed once. `PhysicsIntern.resume` reads the same file, so
those runs could not be resumed either. Physics mode had its own copy of the
loading logic and did write the file, which is why only one mode was affected.
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


def test_the_workspace_writes_the_spec_into_the_initial_commit(tmp_path) -> None:
    from physics_intern.core.config import Config
    from physics_intern.core.workspace import WorkspaceManager

    root = tmp_path / "ws"
    root.mkdir()
    config = Config()
    config.workspace_dir = str(root)
    WorkspaceManager(config).init("Measure the half-life.", problem_def=SPEC)

    assert (root / "problem.yaml").exists(), (
        "the evaluator reads problem.yaml out of the workspace — a run that "
        "does not write it is silently never scored"
    )
    assert yaml.safe_load((root / "problem.yaml").read_text())["name"] == "decay_curve"


def test_a_run_without_a_spec_still_initializes(tmp_path) -> None:
    from physics_intern.core.config import Config
    from physics_intern.core.workspace import WorkspaceManager

    root = tmp_path / "ws"
    root.mkdir()
    config = Config()
    config.workspace_dir = str(root)
    WorkspaceManager(config).init("Why is the sky blue?")
    assert (root / "RESEARCH_STATE.md").exists()
    assert not (root / "problem.yaml").exists()


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
    """Four near-copies is how research mode lost the spec in the first place."""
    calls = _call_kwargs(path, function, "run_problem")
    assert calls, (
        f"{path}:{function} does not call physics_intern.run.run_problem — a "
        "second copy of the run logic is how the modes drifted apart before"
    )
    for kwargs in calls:
        assert "mode" in kwargs


@pytest.mark.parametrize("mode", ["physics", "research"])
def test_the_runner_passes_the_spec_in_both_modes(mode, monkeypatch, tmp_path) -> None:
    """The one place that has to get it right, now that it is the only place."""
    import physics_intern.run as run_module

    seen: dict = {}

    def fake_autophysicist(**kwargs):
        seen.update(kwargs)
        return tmp_path

    class FakeEngine:
        def __init__(self, text, **kwargs):
            seen.update(kwargs)
            seen["text"] = text
            self.workspace = type("W", (), {"root": tmp_path})()

        def run(self):
            pass

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_autophysicist
    )
    monkeypatch.setattr("physics_intern.engine.PhysicsIntern", FakeEngine)

    spec_path = tmp_path / "problem.yaml"
    spec_path.write_text(yaml.dump(SPEC), encoding="utf-8")
    run_module.run_problem(str(spec_path), mode=mode)

    if mode == "physics":
        assert seen["problem_def"] == SPEC
        assert seen["problem_name"] == "decay_curve"
        assert seen["answer_template"].startswith("def answer")
    else:
        assert seen["problem_def"] == SPEC
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
