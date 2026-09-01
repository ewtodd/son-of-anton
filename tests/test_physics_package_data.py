"""Every prompt the package loads at runtime must ship with the package.

These are read with ``Path(__file__).parent / "<name>.md"``. A file missing
from ``[tool.setuptools.package-data]`` is not a build error and not an import
error — it is a FileNotFoundError in a sealed install only, at the moment that
agent first runs, which for a critic that fires at the end of iteration one
means the run is already well underway.

That has happened twice: `problems/` was never shipped at all, and
`autophysicist/critic_prompt.md` was missed because the manifest enumerated
prompt filenames one by one. Hence globs, and hence this test.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "physics_intern"


def _patterns() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["setuptools"]["package-data"]["physics_intern"]


def _data_files() -> list[Path]:
    """Non-Python files inside the package that runtime code could read."""
    return [
        path
        for path in PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix in {".md", ".yaml", ".yml"}
        and "__pycache__" not in path.parts
    ]


def test_the_package_actually_contains_data_files() -> None:
    assert _data_files(), "no prompts found — has the package moved?"


@pytest.mark.parametrize(
    "relative",
    [str(p.relative_to(PACKAGE)) for p in _data_files()],
    ids=lambda r: r,
)
def test_every_data_file_is_shipped(relative: str) -> None:
    patterns = _patterns()
    assert any(fnmatch.fnmatch(relative, pattern) for pattern in patterns), (
        f"{relative} is not matched by any package-data pattern in "
        f"pyproject.toml ({patterns}). It will be missing from the sealed "
        f"install and whatever loads it will raise FileNotFoundError at "
        f"runtime — not at build or import time."
    )


def test_the_prompts_that_are_loaded_by_name_exist() -> None:
    """Guards the other direction: a loader pointing at a file that is gone."""
    for module, filename in (
        ("autophysicist", "prompt.md"),
        ("autophysicist", "critic_prompt.md"),
    ):
        assert (PACKAGE / module / filename).is_file(), (
            f"physics_intern/{module}/{filename} is loaded at runtime but "
            f"does not exist"
        )
