"""The #44585 cron model-drift fail-closed guard and its opt-out.

Unpinned cron jobs (no explicit ``provider``/``model``) follow the global
default, which can change after creation. The guard fails closed on that
change to stop unattended jobs silently inheriting a *paid* default — the
$7.73 overage incident named both a provider and a model switch.

The opt-out ``cron.model_drift_guard: false`` is the intended path for
setups with no spend to protect against (local models): with it off,
``cron_model_drift_axes`` reports no drift, the scheduler's drift block
never fires, and the job runs on the live configured default.

These tests lock that contract:
  * the guard is fail-closed by default (missing / non-boolean -> on),
  * only the literal boolean ``false`` disables it,
  * with the guard on, an unpinned axis whose snapshot differs from the
    current default is reported as drift,
  * with the guard off, the SAME drift is reported as no drift (job tracks
    the live default),
  * a pinned axis is never drift, and an axis with no snapshot (back-compat)
    is never drift.
"""

from __future__ import annotations

from son_of_anton_cli.config import (
    cron_model_drift_axes,
    cron_model_drift_guard_enabled,
)


def _cfg(guard):
    return {"cron": {"model_drift_guard": guard}}


# ── cron_model_drift_guard_enabled: fail-closed default ──────────────────

def test_guard_enabled_by_default_when_config_absent() -> None:
    assert cron_model_drift_guard_enabled({}) is True


def test_guard_enabled_when_true() -> None:
    assert cron_model_drift_guard_enabled(_cfg(True)) is True


def test_guard_disabled_only_by_literal_false() -> None:
    assert cron_model_drift_guard_enabled(_cfg(False)) is False


def test_guard_stays_on_for_non_boolean_values() -> None:
    # Malformed / non-boolean must not silently disable a spend-safety guard.
    assert cron_model_drift_guard_enabled(_cfg("false")) is True
    assert cron_model_drift_guard_enabled(_cfg("yes")) is True
    assert cron_model_drift_guard_enabled(_cfg(None)) is True
    assert cron_model_drift_guard_enabled(_cfg(0)) is True


# ── cron_model_drift_axes: the drift predicate ───────────────────────────

# An unpinned job created when the default was old-provider/old-model, now
# that the global default has moved to new-provider/new-model.
_UNPINNED_JOB = {
    "provider_snapshot": "old-provider",
    "model_snapshot": "old-model",
}
_CURRENT = dict(current_provider="new-provider", current_model="new-model")


def test_guard_on_reports_drifted_unpinned_axes() -> None:
    axes = cron_model_drift_axes(_UNPINNED_JOB, config=_cfg(True), **_CURRENT)
    assert set(axes) == {"provider", "model"}


def test_guard_off_reports_no_drift_job_tracks_live_default() -> None:
    # The whole point of the opt-out: identical drift, no reported axes, so
    # the scheduler runs the job on the current default instead of skipping.
    axes = cron_model_drift_axes(_UNPINNED_JOB, config=_cfg(False), **_CURRENT)
    assert axes == []


def test_pinned_axis_is_never_drift() -> None:
    # The model axis is explicitly pinned, so it is not drift even though its
    # snapshot differs; the unpinned provider axis still is.
    job = dict(_UNPINNED_JOB, model="pinned-model")
    axes = cron_model_drift_axes(job, config=_cfg(True), **_CURRENT)
    assert axes == ["provider"]


def test_axis_without_snapshot_is_never_drift() -> None:
    # Back-compat: pre-existing / no_agent jobs with no snapshot on an axis
    # behave exactly as before — the guard never engages for that axis.
    job = {"model_snapshot": "old-model"}  # provider has no snapshot
    axes = cron_model_drift_axes(job, config=_cfg(True), **_CURRENT)
    assert axes == ["model"]


def test_no_drift_when_default_unchanged() -> None:
    job = {"provider_snapshot": "same", "model_snapshot": "same"}
    axes = cron_model_drift_axes(
        job,
        config=_cfg(True),
        current_provider="same",
        current_model="same",
    )
    assert axes == []


def test_cron_fleet_default_suppresses_its_axis() -> None:
    # An axis covered by an explicit cron.model / cron.model_provider is a
    # deliberate routing, not drift — the guard is skipped for that axis.
    cfg = _cfg(True)
    cfg["cron"]["model"] = "fleet-model"
    job = {"provider_snapshot": "old-provider"}  # model axis fleet-covered
    axes = cron_model_drift_axes(
        job,
        config=cfg,
        current_provider="new-provider",
        current_model="whatever",
    )
    # Model axis suppressed by the fleet default; provider axis still drifts.
    assert axes == ["provider"]
