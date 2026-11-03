"""Every prompt the package loads at runtime must exist next to its code.

These are read with ``Path(__file__).parent / "<name>.md"``. A file missing
from the installed package is not a build error and not an import error — it
is a FileNotFoundError at the moment that agent first runs, which for a critic
that fires at the end of iteration one means the run is already well underway.
The Nix package installs the whole ``autophysicist/`` directory (nix/
physics-harness.nix), so what is needed here is that the loaders point at
files that exist.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def test_the_prompts_that_are_loaded_by_name_exist() -> None:
    for filename in ("prompt.md", "critic_prompt.md"):
        assert (PACKAGE / filename).is_file(), (
            f"autophysicist/{filename} is loaded at runtime but does not exist"
        )


def test_the_default_config_ships_and_parses() -> None:
    with (PACKAGE / "config.default.toml").open("rb") as handle:
        assert tomllib.load(handle)
