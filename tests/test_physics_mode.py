"""Physics-mode contracts: the modules import cleanly, the OpenAI-compatible
endpoint layer resolves config.yaml in the documented order, and the formal
evaluation scores numeric checks against workspace RESULTS.txt.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from physics_intern.llm import _resolve_endpoint


def _base_url(client) -> str:
    """Return the client's endpoint with the httpx trailing slash normalized."""
    return str(client.base_url).rstrip("/")


def test_physics_modules_import() -> None:
    importlib.import_module("physics_intern")
    importlib.import_module("physics_intern.run")
    importlib.import_module("physics_intern.autophysicist")
    importlib.import_module("physics_intern.llm")
    importlib.import_module("physics_intern.verification.experimental")


def test_config_builds_for_unregistered_models() -> None:
    # The fork ships no models.yaml: Config must fall back to the package
    # default max_tokens instead of refusing to run (smoke-test regression).
    from physics_intern.core.config import Config

    config = Config()
    assert config.max_tokens > 0


def test_build_config_accepts_programmatic_overrides() -> None:
    # The Autophysicist runner passes overrides=... (smoke-test regression).
    from physics_intern.core.config import build_config

    config = build_config(None, overrides={"model": "qwen3.6-35b-a3b"})
    assert config.model == "qwen3.6-35b-a3b"
    assert config.max_tokens > 0


def test_formal_evaluation_render_does_not_raise(tmp_path: Path) -> None:
    # render_formal_evaluation imports the fork's console module
    # (physics_intern.core.console) — this was a live ModuleNotFoundError
    # before the smoke test (smoke-test regression).
    from physics_intern.verification.experimental import (
        run_formal_evaluation,
        render_formal_evaluation,
    )

    problem = {
        "checks": [
            {"id": "halflife", "key": "halflife_s", "expected": 119.2, "tolerance": 4.0},
        ],
    }
    (tmp_path / "RESULTS.txt").write_text("halflife_s = 120.0\n", encoding="utf-8")
    result = run_formal_evaluation(str(tmp_path), problem)
    render_formal_evaluation(result)  # must not raise


def test_endpoint_explicit_physics_base_url_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        "physics_intern.llm._load_agent_config",
        lambda: {
            "model": {"provider": "deepseek", "default": "deepseek-v4"},
            "physics": {
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "deepseek-v4",
            },
            "custom_providers": {},
        },
    )

    class _Config:
        model = ""
        api_timeout = 5.0

    client, model = _resolve_endpoint(_Config())
    assert model == "deepseek-v4"
    assert _base_url(client) == "http://127.0.0.1:9999/v1"


def test_endpoint_deepseek_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "physics_intern.llm._load_agent_config",
        lambda: {
            "model": {"provider": "deepseek", "default": "deepseek-v4"},
            "physics": {},
            "custom_providers": {},
        },
    )

    class _Config:
        model = ""
        api_timeout = 5.0

    client, _ = _resolve_endpoint(_Config())
    assert _base_url(client) == "https://api.deepseek.com/v1"


def test_endpoint_openai_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "physics_intern.llm._load_agent_config",
        lambda: {
            "model": {"provider": "openai", "default": "gpt-5"},
            "physics": {},
            "custom_providers": {},
        },
    )

    class _Config:
        model = ""
        api_timeout = 5.0

    client, model = _resolve_endpoint(_Config())
    assert model == "gpt-5"
    assert _base_url(client) == "https://api.openai.com/v1"


def test_endpoint_custom_provider_then_localhost_fallback(monkeypatch) -> None:
    config = {
        "model": {"provider": "llama-swap", "default": "deepseek-v4"},
        "physics": {},
        "custom_providers": {
            "llama-swap": {"base_url": "http://127.0.0.1:8080/v1"},
        },
    }
    monkeypatch.setattr("physics_intern.llm._load_agent_config", lambda: config)

    class _Config:
        model = ""
        api_timeout = 5.0

    client, _ = _resolve_endpoint(_Config())
    assert _base_url(client) == "http://127.0.0.1:8080/v1"

    # Unknown provider with no custom entry falls back to the localhost default.
    config["custom_providers"] = {}
    client, _ = _resolve_endpoint(_Config())
    assert _base_url(client) == "http://127.0.0.1:8080/v1"


def test_formal_evaluation_passes_and_fails_numeric_checks(tmp_path: Path) -> None:
    from physics_intern.verification.experimental import run_formal_evaluation

    problem = {
        "checks": [
            {"id": "mass", "key": "mass", "expected": 0.511, "tolerance": 0.01},
            {"id": "exact", "key": "count", "expected": 42},
        ],
    }

    (tmp_path / "RESULTS.txt").write_text(
        "mass = 0.5110\ncount = 42.0\n# comment line\n", encoding="utf-8"
    )
    result = run_formal_evaluation(str(tmp_path), problem)
    assert result.passed is True
    assert result.passed_count == 2

    (tmp_path / "RESULTS.txt").write_text("mass = 0.600\n", encoding="utf-8")
    result = run_formal_evaluation(str(tmp_path), problem)
    assert result.passed is False
    assert result.passed_count == 0  # mass out of tolerance, count missing
    assert "failed checks" in result.message


def test_formal_evaluation_missing_value_fails_check(tmp_path: Path) -> None:
    from physics_intern.verification.experimental import run_formal_evaluation

    problem = {
        "checks": [
            {"id": "mass", "key": "mass", "expected": 0.511, "tolerance": 0.01},
        ],
    }
    # No RESULTS.txt at all → the check fails, the run reports a failed check.
    result = run_formal_evaluation(str(tmp_path), problem)
    assert result.passed is False
    assert result.checks[0]["passed"] is False


def test_verification_spec_checks_have_required_fields() -> None:
    # Every toy problem spec must carry numeric checks with keys + expected
    # values so formal evaluation is meaningful.
    import yaml

    problems_root = Path(__file__).resolve().parent.parent / "problems"
    if not problems_root.is_dir():
        return
    problem_files = list(problems_root.rglob("problem.yaml"))
    assert problem_files, "problems/ should ship at least one problem.yaml"
    for path in problem_files:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert spec.get("checks"), f"{path} has no numeric checks"
        for check in spec["checks"]:
            assert check.get("key"), f"{path}: check missing key"
            assert "expected" in check, f"{path}: check {check['id']} missing expected"
