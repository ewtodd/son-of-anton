"""Guards for the profile-multiplexing excision.

One gateway process serves one ``SON_OF_ANTON_HOME``. The multiplexer that
served many profiles from one process is what made the split-brain session bug
possible: the agent's write path resolved a profile-scoped home while the
transcript read path resolved the root home, so every turn wrote one database
and read another. These tests fail if any of that machinery comes back.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

REMOVED_MODULES = (
    "son_of_anton_cli.profiles",
    "son_of_anton_cli.profile_distribution",
    "son_of_anton_cli.profile_describer",
    "son_of_anton_cli.subcommands.profile",
    "gateway.profile_routing",
    "gateway.profile_pins",
)

# Directories with their own dependency story — vendored code and build output.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "result"}


def _python_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts)
    ]


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_profile_modules_are_gone(module: str) -> None:
    """None of the profile modules may become importable again."""
    try:
        importlib.import_module(module)
    except ImportError:
        return
    raise AssertionError(f"{module} is importable again — profiles resurrected")


def test_nothing_imports_the_removed_profile_modules() -> None:
    """A stale import is invisible to the compiler until that line executes.

    Two silent ``ImportError``s reached a green tree during this excision by
    exactly that route, so the import graph is checked statically rather than
    relying on a module happening to be imported by some test.
    """
    offenders: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                name = node.module or ""
            elif isinstance(node, ast.Import):
                name = ",".join(alias.name for alias in node.names)
            else:
                continue
            for removed in REMOVED_MODULES:
                if name == removed or name.startswith(removed + "."):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{node.lineno} imports {removed}")
    assert not offenders, "removed profile modules are still imported:\n" + "\n".join(
        offenders
    )


def test_session_keys_use_the_single_namespace() -> None:
    """Keys must be byte-identical to the pre-multiplex format again.

    ``build_session_key`` briefly carried a ``profile`` parameter that swapped
    ``agent:main`` for ``agent:<profile>``. Every positional parser downstream
    reads ``parts[2]`` as the platform, so the namespace has to stay one slot.
    """
    import inspect

    from gateway.session import (
        SESSION_KEY_NAMESPACE,
        Platform,
        SessionSource,
        build_session_key,
    )

    assert SESSION_KEY_NAMESPACE == "agent:main"
    assert "profile" not in inspect.signature(build_session_key).parameters

    key = build_session_key(
        SessionSource(
            platform=Platform.SIGNAL,
            chat_id="group-1",
            chat_type="group",
            user_id="u1",
        )
    )
    assert key.startswith("agent:main:signal:")


def test_session_source_carries_no_profile() -> None:
    """The field that selected a per-turn home and secret scope is gone."""
    from gateway.session import SessionSource

    fields = SessionSource.__dataclass_fields__
    assert "profile" not in fields
    assert "profile_route_rejected" not in fields


def test_secret_scope_exposes_no_scoping_api() -> None:
    """``get_secret`` is the credential seam; the scope around it is gone.

    The context-local scope existed only so one process could serve several
    profiles' credentials without unioning them into ``os.environ``. Keeping
    any part of it would invite a second, divergent resolution path.
    """
    import agent.secret_scope as secret_scope

    for name in (
        "set_secret_scope",
        "reset_secret_scope",
        "current_secret_scope",
        "build_profile_secret_scope",
        "set_multiplex_active",
        "is_multiplex_active",
        "UnscopedSecretError",
        "_is_global_env",
    ):
        assert not hasattr(secret_scope, name), (
            f"agent.secret_scope.{name} is back — the fail-closed profile scope "
            "was removed with multiplexing"
        )

    assert hasattr(secret_scope, "get_secret")
    assert hasattr(secret_scope, "load_env_file")


def test_gateway_config_has_no_multiplex_settings() -> None:
    """config.yaml must not be able to turn multiplexing back on."""
    from gateway.config import GatewayConfig

    fields = GatewayConfig.__dataclass_fields__
    for name in (
        "multiplex_profiles",
        "multiplex_profile_allowlist",
        "profile_routes",
    ):
        assert name not in fields, f"GatewayConfig.{name} is back"


def test_getenv_reads_the_process_environment(monkeypatch) -> None:
    """Gateway env reads resolve from os.environ, with no scope in between."""
    from gateway.config import _getenv

    monkeypatch.setenv("SON_OF_ANTON_TEST_TOKEN", "  value  ")
    assert _getenv("SON_OF_ANTON_TEST_TOKEN") == "  value  "
    monkeypatch.delenv("SON_OF_ANTON_TEST_TOKEN")
    assert _getenv("SON_OF_ANTON_TEST_TOKEN", "fallback") == "fallback"
