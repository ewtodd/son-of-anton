"""Regression tests for #4707 — cron must be per-profile.

Design intent (Teknium, June 2026): a profile's cron jobs both LIVE in that
profile's RENCO_HOME and EXECUTE under it.

- Storage: a job created under profile ``coder`` writes to
  ``~/.renco/profiles/coder/cron/jobs.json`` — NOT the shared default root.
- Execution: the profile-scoped gateway's in-process ticker resolves the
  active RENCO_HOME (profile home) at call time, so jobs run with that
  profile's ``.env`` / ``config.yaml`` / scripts / skills.

This is the opposite direction from the (reverted) #50112/#32091 "anchor at the
shared root" approach. Anchoring at the root funnels every profile's jobs into
one store and runs them under whatever RENCO_HOME the ticker happens to have —
leaking config/credentials/skills across profiles, the security boundary #4707
was filed for. These tests pin per-profile isolation so a stale-branch merge or
a re-anchor "fix" can't silently flip it back.
"""
import importlib
from pathlib import Path


def _set_profile_env(monkeypatch, root: Path, profile_home: Path) -> None:
    """Pretend the platform default root is ``root`` and the active
    RENCO_HOME is a profile under it (``<root>/profiles/<name>``)."""
    import renco_constants

    monkeypatch.setattr(
        renco_constants, "_get_platform_default_renco_home", lambda: root
    )
    monkeypatch.setenv("RENCO_HOME", str(profile_home))


def test_cron_storage_anchors_at_profile_home(tmp_path, monkeypatch):
    """Under a profile RENCO_HOME (<root>/profiles/<name>), the cron store
    resolves to <profile>/cron, NOT the shared <root>/cron."""
    root = tmp_path / "renco_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    _set_profile_env(monkeypatch, root, profile_home)

    import renco_constants

    # Sanity: the override is wired the way the gateway sees it.
    assert renco_constants.get_renco_home().resolve() == profile_home.resolve()
    assert renco_constants.get_default_renco_root().resolve() == root.resolve()

    # cron/jobs.py computes RENCO_DIR from get_renco_home() at import, so a
    # fresh import under this env anchors the store at <profile>/cron.
    import cron.jobs as jobs

    importlib.reload(jobs)
    try:
        assert jobs.RENCO_DIR.resolve() == profile_home.resolve()
        assert (
            jobs.JOBS_FILE.resolve()
            == (profile_home / "cron" / "jobs.json").resolve()
        )
        # The shared-root path must NOT be the store — that would re-break
        # per-profile isolation (#4707).
        assert (
            jobs.JOBS_FILE.resolve() != (root / "cron" / "jobs.json").resolve()
        )
    finally:
        monkeypatch.undo()
        importlib.reload(jobs)


