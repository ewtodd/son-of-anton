"""Profile isolation contracts — every home-path accessor scopes to the
active SON_OF_ANTON_HOME (never a hardcoded ~/.son-of-anton).
"""

from __future__ import annotations

from pathlib import Path

from son_of_anton_constants import (
    display_son_of_anton_home,
    get_son_of_anton_home,
)


def test_home_scopes_to_env_override() -> None:
    expected = Path(environ_home()).resolve()
    assert get_son_of_anton_home() == expected


def test_display_home_matches_resolved_home() -> None:
    assert display_son_of_anton_home() == str(get_son_of_anton_home())


def test_home_does_not_point_at_real_user_home() -> None:
    # The autouse fixture guarantees the active home is a temp dir; the
    # accessor must never fall back to the developer's real ~/.son-of-anton.
    real = Path.home() / ".son-of-anton"
    assert get_son_of_anton_home() != real


def environ_home() -> str:
    import os

    return os.environ["SON_OF_ANTON_HOME"]
