"""Subprocess HOME contract — terminal.home_mode=cwd must make ``~`` resolve
to the command's working directory (gateway profiles where cwd is the
user's home).
"""

from __future__ import annotations

from son_of_anton_constants import get_subprocess_home


def test_cwd_mode_uses_injected_marker() -> None:
    home = get_subprocess_home(
        {
            "TERMINAL_HOME_MODE": "cwd",
            "SON_OF_ANTON_SUBPROCESS_CWD": "/home/e-work",
            "HOME": "/var/lib/son-of-anton",
        }
    )
    assert home == "/home/e-work"


def test_cwd_mode_falls_back_to_none_without_marker() -> None:
    assert (
        get_subprocess_home({"TERMINAL_HOME_MODE": "cwd"}) is None
    )


def test_auto_mode_is_unchanged() -> None:
    home = get_subprocess_home({"TERMINAL_HOME_MODE": "auto"})
    assert home != "/home/e-work" or home is None  # auto never forces cwd


def test_working_dir_alias() -> None:
    home = get_subprocess_home(
        {
            "TERMINAL_HOME_MODE": "working_dir",
            "SON_OF_ANTON_SUBPROCESS_CWD": "/home/e-play",
        }
    )
    assert home == "/home/e-play"
