"""Shared fixtures for the son-of-anton test suite.

Every test runs with a temp ``SON_OF_ANTON_HOME`` so no test can touch the
real ``~/.son-of-anton`` (see AGENTS.md "Tests must not write to
~/.son-of-anton/"). Credential env vars are blanked to mirror the hermetic
environment ``scripts/run_tests.sh`` already enforces.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Env vars that must never leak from the host into a test run.
_CREDENTIAL_ENV_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "SEARXNG_URL",
    "WEB_EXTRACT_URL",
    "GITHUB_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SIGNAL_HTTP_URL",
]


@pytest.fixture(autouse=True)
def _isolate_son_of_anton_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point SON_OF_ANTON_HOME at a temp dir and blank credential env vars."""
    with tempfile.TemporaryDirectory(prefix="soa-test-home-") as tmp:
        monkeypatch.setenv("SON_OF_ANTON_HOME", tmp)
        for var in _CREDENTIAL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        yield


@pytest.fixture
def son_of_anton_home() -> Path:
    """The temp SON_OF_ANTON_HOME path (post-isolation)."""
    return Path(os.environ["SON_OF_ANTON_HOME"])
