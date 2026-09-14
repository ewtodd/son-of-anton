"""``gateway.single_user`` and the three gateway fixes that shipped with it.

  * A single-user account browses and resumes its whole session database
    from any surface, the way the terminal already does; the flag is refused
    unless the platform allowlists name exactly one person.
  * A persisted /model override whose model the provider no longer serves is
    dropped on the provider's own error instead of failing every turn.
  * The held-messages ask happens when the active-hours window opens, not
    only when someone writes again.
  * A stop issued by the service manager exits cleanly; an unexpected signal
    from elsewhere still exits non-zero for Restart=on-failure supervisors.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from gateway.single_user import allowlist_problem


def _runner(**config):
    from gateway.run import GatewayRunner

    obj = object.__new__(GatewayRunner)
    obj.config = types.SimpleNamespace(**config)
    return obj


def _source(chat_id="group:1", user_id="+1555"):
    return types.SimpleNamespace(
        chat_id=chat_id, user_id=user_id, chat_type="group",
        platform=types.SimpleNamespace(value="signal"),
    )


# ── the flag ──────────────────────────────────────────────────────────────

def test_config_reads_single_user_from_the_gateway_section() -> None:
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict({"single_user": True})
    assert cfg.single_user is True
    assert cfg.to_dict()["single_user"] is True
    assert GatewayConfig.from_dict({}).single_user is False


def test_single_user_sees_every_row_and_target() -> None:
    gw = _runner(single_user=True)
    gw._resume_caller_is_admin = lambda source: False
    row = {"id": "20260825_090000_aaaaaaaa", "source": "cli"}
    assert asyncio.run(gw._resume_row_visible(_source(), row, allow_all=False))
    assert asyncio.run(gw._resume_target_allowed(_source(), row["id"]))


def test_a_shared_instance_still_needs_an_admin() -> None:
    gw = _runner(single_user=False)
    gw._resume_caller_is_admin = lambda source: False
    gw._gateway_session_origin_for_id = lambda sid: None
    gw._session_db = types.SimpleNamespace(get_session=_none_async)
    row = {"id": "20260825_090000_aaaaaaaa", "source": "cli"}
    assert not asyncio.run(gw._resume_row_visible(_source(), row, allow_all=True))


async def _none_async(*a, **k):
    return None


# ── the allowlist guard ───────────────────────────────────────────────────

def _signal(dm, group=()):
    return types.SimpleNamespace(dm_allow_from=set(dm), group_allow_from=set(group))


def test_one_named_user_across_every_platform_is_single_user() -> None:
    assert allowlist_problem({"signal": _signal({"+1555"}, {"+1555"})}) is None


def test_open_empty_or_plural_allowlists_are_refused() -> None:
    assert "open" in allowlist_problem({"signal": _signal({"*"})})
    assert "0 users" in allowlist_problem({"signal": _signal(set())})
    assert "2 users" in allowlist_problem({"signal": _signal({"+1555", "+1666"})})


def test_an_adapter_without_a_readable_allowlist_is_refused() -> None:
    problem = allowlist_problem({"discord": types.SimpleNamespace()})
    assert "cannot read" in problem and "discord" in problem


def test_the_runner_clears_the_flag_when_the_allowlist_disagrees(caplog) -> None:
    gw = _runner(single_user=True)
    gw.adapters = {"signal": _signal({"*"})}
    gw._enforce_single_user_allowlist()
    assert gw.config.single_user is False
    gw = _runner(single_user=True)
    gw.adapters = {"signal": _signal({"+1555"})}
    gw._enforce_single_user_allowlist()
    assert gw.config.single_user is True


# ── dead /model override ──────────────────────────────────────────────────

def _override_runner(model="qwen3.8-flash-next-local"):
    gw = _runner()
    state = types.SimpleNamespace(
        conversation=types.SimpleNamespace(model_override={"model": model, "provider": "custom"})
    )
    gw._peek_session_state = lambda key: state
    cleared, evicted, notices = [], [], []
    gw.session_store = types.SimpleNamespace(
        set_model_override=lambda key, value: cleared.append((key, value))
    )
    gw._evict_cached_agent = lambda key: evicted.append(key)

    async def _notice(origin, text):
        notices.append((origin, text))

    gw._deliver_platform_notice = _notice
    return gw, state, cleared, evicted, notices


def test_a_missing_model_drops_the_override_and_tells_the_chat() -> None:
    gw, state, cleared, evicted, notices = _override_runner()
    entry = types.SimpleNamespace(origin=_source())
    err = "Error code: 400 - {'error': {'message': '/chat/completions: Invalid model name passed in model=qwen3.8-flash-next-local'}}"
    assert asyncio.run(gw._drop_dead_model_override("k", entry, err)) is True
    assert state.conversation.model_override is None
    assert cleared == [("k", None)] and evicted == ["k"]
    assert notices and "qwen3.8-flash-next-local" in notices[0][1]


def test_other_failures_leave_the_override_alone() -> None:
    gw, state, cleared, evicted, notices = _override_runner()
    entry = types.SimpleNamespace(origin=_source())
    assert asyncio.run(gw._drop_dead_model_override("k", entry, "rate limit exceeded")) is False
    assert state.conversation.model_override is not None
    assert cleared == [] and notices == []


def test_no_override_means_nothing_to_drop() -> None:
    gw = _runner()
    gw._peek_session_state = lambda key: None
    assert asyncio.run(gw._drop_dead_model_override("k", None, "invalid model")) is False


# ── the held-messages ask on reopen ───────────────────────────────────────

def test_window_open_offers_held_messages_once_per_chat(tmp_path: Path) -> None:
    from gateway import held_messages as held

    held.save_message(tmp_path, "k1", sender="alice", text="are you there?")
    held.save_message(tmp_path, "k2", sender="bob", text="ping")
    gw = _runner(active_hours=(20, 7), sessions_dir=tmp_path)
    origin1 = _source("group:1")
    gw.session_store = types.SimpleNamespace(
        entries_snapshot=lambda: {
            "k1": types.SimpleNamespace(origin=origin1),
            "k2": types.SimpleNamespace(origin=None),  # no live origin: skipped
        }
    )
    states = {}

    def _state(key):
        return states.setdefault(
            key, types.SimpleNamespace(persistent=types.SimpleNamespace(held_messages_pending=False))
        )

    gw._session_state = _state
    sent = []

    async def _notice(origin, text):
        sent.append((origin, text))

    gw._deliver_platform_notice = _notice
    assert asyncio.run(gw._offer_held_messages_on_reopen()) == 1
    assert sent[0][0] is origin1 and "alice" in sent[0][1]
    assert states["k1"].persistent.held_messages_pending is True
    # already pending → not asked twice
    assert asyncio.run(gw._offer_held_messages_on_reopen()) == 0


def test_the_watch_only_starts_for_windowed_instances() -> None:
    gw = _runner(active_hours=None)
    gw._start_active_hours_reopen_watch()
    assert not hasattr(gw, "_active_hours_watch_task")


# ── exit status ───────────────────────────────────────────────────────────

def test_service_manager_stops_exit_clean_and_stray_kills_do_not() -> None:
    from gateway.run import exit_status_after_signal as clean

    assert clean(signal_initiated=False, restart_requested=False, from_service_manager=False)
    assert clean(signal_initiated=True, restart_requested=True, from_service_manager=False)
    assert clean(signal_initiated=True, restart_requested=False, from_service_manager=True)
    assert not clean(signal_initiated=True, restart_requested=False, from_service_manager=False)
