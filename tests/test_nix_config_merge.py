"""The activation-time config.yaml merge can retract a key.

The bug this covers: the merge was two-way — on-disk as base, Nix settings
layered on top — so it could add and overwrite but never remove. A key Nix
stopped declaring stayed on disk forever, which meant `nixos-rebuild` reported
retiring a model while the model went on being offered to the agent and every
request against it failed at the provider. Nothing surfaced the discrepancy;
the rebuild had, as far as it knew, succeeded.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MERGE_PY = REPO_ROOT / "nix" / "config_merge.py"


@pytest.fixture(scope="module")
def merge_module():
    """Import nix/config_merge.py by path — it ships as a Nix script, not a module."""
    spec = importlib.util.spec_from_file_location("nix_config_merge", MERGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_merge(merge_module, tmp_path, nix_settings, *, adopt=False, dry_run=False):
    """Invoke the merge the way activation does, returning the resulting config."""
    nix_json = tmp_path / "nix.json"
    nix_json.write_text(json.dumps(nix_settings))
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / ".nix-managed.json"

    argv = [str(nix_json), str(config_path), "--state", str(state_path)]
    if adopt:
        argv.append("--adopt")
    if dry_run:
        argv.append("--dry-run")

    import sys

    saved = sys.argv
    sys.argv = ["config_merge"] + argv
    try:
        assert merge_module.main() == 0
    finally:
        sys.argv = saved

    return yaml.safe_load(config_path.read_text()) or {}


def write_config(tmp_path, data):
    (tmp_path / "config.yaml").write_text(yaml.dump(data, sort_keys=False))


def test_nix_keys_are_applied(merge_module, tmp_path) -> None:
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["model"]["default"] == "qwen"


def test_nix_overwrites_a_stale_on_disk_value(merge_module, tmp_path) -> None:
    write_config(tmp_path, {"model": {"default": "old"}})
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["model"]["default"] == "qwen"


def test_retired_key_is_removed_on_the_next_activation(merge_module, tmp_path) -> None:
    first = {"custom_providers": {"custom": {"models": {"qwen": {}, "gemma": {}}}}}
    run_merge(merge_module, tmp_path, first)
    second = {"custom_providers": {"custom": {"models": {"qwen": {}}}}}
    result = run_merge(merge_module, tmp_path, second)
    models = result["custom_providers"]["custom"]["models"]
    assert "gemma" not in models, (
        "Nix stopped declaring gemma, so it must not survive on disk — this is "
        "the whole reason the merge keeps a state file"
    )
    assert "qwen" in models


def test_removing_the_last_child_removes_the_empty_parent(
    merge_module, tmp_path
) -> None:
    run_merge(merge_module, tmp_path, {"a": {"b": {"c": 1}}, "keep": 1})
    result = run_merge(merge_module, tmp_path, {"keep": 1})
    assert "a" not in result, "an empty dict left behind is still a stale key"


def test_runtime_written_keys_are_never_touched(merge_module, tmp_path) -> None:
    write_config(tmp_path, {"skills": {"autoload": True}})
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["skills"] == {"autoload": True}
    # ...and still there after a second activation that knows the state file.
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["skills"] == {"autoload": True}


def test_a_value_edited_after_nix_wrote_it_survives_retirement(
    merge_module, tmp_path
) -> None:
    run_merge(merge_module, tmp_path, {"gateway": {"model": "qwen", "extra": "nix"}})
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    config["gateway"]["extra"] = "changed at runtime"
    write_config(tmp_path, config)

    result = run_merge(merge_module, tmp_path, {"gateway": {"model": "qwen"}})
    assert result["gateway"]["extra"] == "changed at runtime", (
        "Nix dropped the key, but something overwrote Nix's value first — that "
        "is a runtime setting now, not Nix's to delete"
    )


def test_first_run_removes_nothing(merge_module, tmp_path) -> None:
    """No state file means no key can be attributed to Nix. Fail safe."""
    write_config(tmp_path, {"legacy": {"model": "gone"}, "model": {"default": "old"}})
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["legacy"] == {"model": "gone"}
    assert result["model"]["default"] == "qwen"


def test_first_run_reports_the_keys_nix_does_not_declare(
    merge_module, tmp_path, capsys
) -> None:
    write_config(tmp_path, {"legacy": {"model": "gone"}})
    run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert "legacy.model" in capsys.readouterr().err


def test_adopt_prunes_everything_nix_does_not_declare(merge_module, tmp_path) -> None:
    write_config(
        tmp_path,
        {
            "custom_providers": {"custom": {"models": {"qwen": {}, "retired": {}}}},
            "gateway": {"multiplex_profiles": True},
        },
    )
    result = run_merge(
        merge_module,
        tmp_path,
        {"custom_providers": {"custom": {"models": {"qwen": {}}}}},
        adopt=True,
    )
    assert result["custom_providers"]["custom"]["models"] == {"qwen": {}}
    assert "gateway" not in result


def test_dry_run_writes_nothing(merge_module, tmp_path) -> None:
    write_config(tmp_path, {"model": {"default": "old"}})
    run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}}, dry_run=True)
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert on_disk["model"]["default"] == "old"
    assert not (tmp_path / ".nix-managed.json").exists()


def test_state_file_records_what_nix_wrote(merge_module, tmp_path) -> None:
    settings = {"model": {"default": "qwen"}}
    run_merge(merge_module, tmp_path, settings)
    assert json.loads((tmp_path / ".nix-managed.json").read_text()) == settings


def test_corrupt_state_file_degrades_to_a_safe_merge(merge_module, tmp_path) -> None:
    write_config(tmp_path, {"legacy": 1})
    (tmp_path / ".nix-managed.json").write_text("{not json")
    result = run_merge(merge_module, tmp_path, {"model": {"default": "qwen"}})
    assert result["legacy"] == 1, "an unreadable state file must not delete anything"
    assert result["model"]["default"] == "qwen"
