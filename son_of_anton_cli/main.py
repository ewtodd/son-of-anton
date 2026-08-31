#!/usr/bin/env python3
"""
Son of Anton CLI - Main entry point.

Usage:
    son-of-anton                     # Interactive chat (default)
    son-of-anton chat                # Interactive chat
    son-of-anton model               # Select the default model
    son-of-anton config              # View configuration
    son-of-anton sessions list       # List past sessions
    son-of-anton cron list           # List cron jobs
    son-of-anton skills              # Manage skills
    son-of-anton mcp                 # Manage MCP servers
    son-of-anton gateway             # Run gateway in foreground
    son-of-anton gateway status      # Show gateway status
    son-of-anton status              # Show status of all components
    son-of-anton pause / resume      # Engage / clear the global emergency stop
    son-of-anton problem create      # Build a physics problem spec from a dataset
    son-of-anton completion bash     # Print a shell completion script
    son-of-anton --version           # Show version

The fork ships no imperative installation surface. There is no `setup`,
`update`, `uninstall`, `login`/`logout`, `doctor` or `gateway install`: the
deployment is declared by the NixOS or Home Manager module, `systemctl` runs
the service, and `settings` in that module owns config.yaml. Commands that
mutated an install were carried over from upstream, already refused under
managed mode, and are gone.
"""

# IMPORTANT: son_of_anton_bootstrap must be the very first import — it hardens
# sys.path and activates the durable lazy-install target.  No-op on POSIX.
#
# Guarded against ModuleNotFoundError because ``son_of_anton_bootstrap`` is a
# top-level module registered via pyproject.toml's ``py-modules`` list.
# When the user upgrades code via ``git pull`` (or ``son-of-anton update``
# crashes between ``git reset --hard`` and ``uv pip install -e .``), the
# new code references ``son_of_anton_bootstrap`` but the editable install's
# ``.pth`` file still points at the old set of top-level modules.  Without
# this guard, son-of-anton crashes on import and the user can't run
# ``son-of-anton update`` to recover.
try:
    import son_of_anton_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass

from son_of_anton_cli.cli_output import line_input

import os
import sys

# ── Startup fast-path bootstrap ─────────────────────────────────────────
# Two lines of inline path math so ``python son_of_anton_cli/main.py`` (script
# mode — sys.path[0] is son_of_anton_cli/, not the repo root) can import the
# canonical helpers; everything else lives in son_of_anton_cli._startup_fast.
_bootstrap_root = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))
if _bootstrap_root not in sys.path:
    sys.path.insert(0, _bootstrap_root)
from son_of_anton_cli import _startup_fast  # noqa: E402

# Early venv self-heal — MUST run before any third-party import below.  When
# a prior ``son-of-anton update`` left a recovery marker and a core package's import
# files were wiped (#57828 — failed lazy backend refresh), the module-level
# ``from son_of_anton_cli.env_loader import ...`` / ``from son_of_anton_cli.config import
# ...`` imports further down would crash before ``main()`` ever reaches
# ``_recover_from_interrupted_install()``.  ``_early_recovery`` is stdlib-only
# (safe to import on a corrupted venv), repairs just enough for this module to
# finish importing, and leaves the marker lifecycle to the full recovery path.
# The module import itself is unguarded on purpose: it lives in this same
# package directory, so if IT can't import, nothing else in son_of_anton_cli can
# either. It is also the canonical home of the probe/repair tables reused by
# the full recovery path below.
from son_of_anton_cli import _early_recovery as _early_recovery_mod

try:
    _early_recovery_mod.recover_if_needed()
except Exception:
    pass


def _exit_after_oneshot(rc: object) -> None:
    """Exit one-shot mode without letting late native finalizers change rc.

    The SIGABRT this guards against (#30387, #43055) fires in a
    native-extension finalizer during CPython's ``Py_FinalizeEx``, *after*
    the response has printed. Flush streams, shut down file logging, then
    ``os._exit`` past interpreter finalization. The ``atexit`` chain is
    deliberately skipped — several handlers re-enter native code that may
    be the abort source. Stateful cleanup is handled in ``_run_agent`` and
    ``_cleanup_oneshot_runtime``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        logging.shutdown()
    except Exception:
        pass
    if rc is None:
        exit_code = 0
    elif isinstance(rc, int):
        exit_code = rc
    else:
        exit_code = 1
    os._exit(exit_code)


_oneshot_cleanup_done = False


def _cleanup_oneshot_runtime() -> None:
    """Best-effort process-global cleanup before one-shot hard exit.

    ``run_oneshot`` owns the agent-local cleanup (memory provider, agent.close,
    session_db.close — all in ``_run_agent``'s finally block). This mirrors the
    process-global pieces from ``cli.py:_run_cleanup()`` that would otherwise
    be skipped by ``os._exit``.
    """
    global _oneshot_cleanup_done
    if _oneshot_cleanup_done:
        return
    _oneshot_cleanup_done = True
    try:
        from tools.terminal_tool import cleanup_all_environments
        cleanup_all_environments()
    except Exception:
        pass
    try:
        from tools.async_delegation import interrupt_all
        interrupt_all(reason="oneshot shutdown")
    except Exception:
        pass
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except BaseException:
        pass
    try:
        from agent.auxiliary_client import shutdown_cached_clients
        shutdown_cached_clients()
    except Exception:
        pass


def _run_and_exit_oneshot(
    prompt: str,
    *,
    model: object = None,
    provider: object = None,
    toolsets: object = None,
    skills: object = None,
    usage_file: object = None,
) -> None:
    try:
        from son_of_anton_cli.oneshot import run_oneshot

        rc = run_oneshot(
            prompt,
            model=model,
            provider=provider,
            toolsets=toolsets,
            skills=skills,
            usage_file=usage_file,
        )
    except KeyboardInterrupt:
        rc = 130
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            rc = 1
        else:
            rc = exc.code
    except BaseException:
        # Defense-in-depth. ``run_oneshot`` already converts agent failures
        # into an int return code and only re-raises KeyboardInterrupt /
        # SystemExit (handled above). Anything still escaping here means
        # ``run_oneshot`` itself malfunctioned — surface it on stderr but never
        # fall through to normal interpreter teardown, which is the exact path
        # that aborts with SIGABRT on AL2023 (the bug this routine fixes).
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        rc = 1
    try:
        _cleanup_oneshot_runtime()
    finally:
        # The hard exit is the safety boundary for #43055. Even an interrupt
        # during best-effort cleanup must not fall back into interpreter
        # finalization, where the reported native SIGABRT occurs.
        _exit_after_oneshot(rc)


def _project_root_str_fast() -> str:
    return _startup_fast.project_root_str()


def _ensure_project_root_on_path_fast() -> None:
    _startup_fast.ensure_project_root_on_path()


def _set_process_title() -> None:
    """Set the process title to 'son-of-anton' so tools like 'ps', 'top', and
    'htop' show the app name instead of 'python3.xx'.

    Purely cosmetic — non-fatal on any platform.

    Strategy (try in order):
      1. ``setproctitle`` (opt-in dep — installed via ``son-of-anton tools`` or
         ``pip install setproctitle``, or bundled in a future release).
      2. ctypes ``prctl(PR_SET_NAME)`` (Linux only, 15-char limit).
      3. ctypes ``pthread_setname_np`` (macOS only, kernel thread name —
         changes lldb/top but not ``ps aux``).
    """
    # Strategy 1: setproctitle (best — works on macOS, Linux, BSD)
    try:
        import setproctitle  # type: ignore[import-untyped]

        setproctitle.setproctitle("son-of-anton")
        return
    except ImportError:
        pass

    # Strategy 2/3: platform-specific ctypes fallback
    import ctypes
    import platform

    try:
        system = platform.system()
        if system == "Linux":
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(15, b"son-of-anton", 0, 0, 0)  # PR_SET_NAME = 15
        elif system == "Darwin":
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            libc.pthread_setname_np(b"son-of-anton")
    except Exception:
        pass


# Son of Anton ships the classic prompt_toolkit CLI only. The Ink TUI was
# removed (2026-08-25): the Nix package never shipped its esbuild bundle, so
# `--tui` could only ever fail with a bogus "workspace missing" error, and the
# CLI covers every surface this project needs.


def _read_openai_version_fast() -> str | None:
    """Read OpenAI SDK version without importing ``importlib.metadata``."""
    return _startup_fast.read_openai_version()


def _print_fast_version_info() -> None:
    _startup_fast.print_fast_version_info()


def _try_ultrafast_version() -> bool:
    """Handle ``son-of-anton --version`` before config/logging imports."""
    return _startup_fast.try_fast_version()


_ensure_project_root_on_path_fast()

if _try_ultrafast_version():
    raise SystemExit(0)

import argparse
import hashlib
import json
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional


import functools as _functools

from son_of_anton_cli.subcommands._shared import add_accept_hooks_flag as _add_accept_hooks_flag
from son_of_anton_cli.subcommands.cron import build_cron_parser
from son_of_anton_cli.subcommands.gateway import build_gateway_parser
from son_of_anton_cli.subcommands.model import build_model_parser

from son_of_anton_cli.subcommands.status import build_status_parser
from son_of_anton_cli.subcommands.pause import build_pause_parser
from son_of_anton_cli.subcommands.config import build_config_parser
from son_of_anton_cli.subcommands.skills import build_skills_parser
from son_of_anton_cli.subcommands.mcp import build_mcp_parser
from son_of_anton_cli.subcommands.problem import build_problem_parser
from son_of_anton_cli.subcommands.completion import build_completion_parser


def _require_tty(command_name: str) -> None:
    """Exit with a clear error if stdin is not a terminal.

    Interactive TUI commands (son-of-anton tools, son-of-anton setup, son-of-anton model) use
    curses or input() prompts that spin at 100% CPU when stdin is a pipe.
    This guard prevents accidental non-interactive invocation.
    """
    if not sys.stdin.isatty():
        print(
            f"Error: 'son-of-anton {command_name}' requires an interactive terminal.\n"
            f"It cannot be run through a pipe or non-interactive subprocess.\n"
            f"Run it directly in your terminal instead.",
            file=sys.stderr,
        )
        sys.exit(1)


# Add project root to path
PROJECT_ROOT = Path(_project_root_str_fast())
_ensure_project_root_on_path_fast()



# Load .env from ~/.son-of-anton/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from son_of_anton_cli.config import get_son_of_anton_home
from son_of_anton_cli.env_loader import load_son_of_anton_dotenv

# Updating dependencies must not import optional secret-manager libraries into
# the updater process before ``uv`` replaces the environment: Bitwarden's
# cryptography import maps a native module and the parent updater then
# prevents its own child installer from replacing that file (#73381).  Profile
# flags have already been stripped above, so the first remaining argument is
# the authoritative argparse subcommand.  Dotenv/managed config still loads;
# only external secret fetches are unnecessary for installation maintenance.
load_son_of_anton_dotenv(
    project_env=PROJECT_ROOT / ".env",
    load_external_secrets=sys.argv[1:2] != ["update"],
)

# Bridge security.redact_secrets from config.yaml → SON_OF_ANTON_REDACT_SECRETS env
# var BEFORE son_of_anton_logging imports agent.redact (which snapshots the flag at
# module-import time). Without this, config.yaml's toggle is ignored because
# the setup_logging() call below imports agent.redact, which reads the env var
# exactly once. Env var in .env still wins — this is config.yaml fallback only.
#
# We also read network.force_ipv4 from the same yaml load to avoid two
# separate config.yaml reads (saves ~17ms on every CLI startup — the second
# `load_config()` was doing a full deep-merge for one boolean lookup).
_FORCE_IPV4_EARLY = False
try:
    # Reuse read_raw_config()'s (mtime, size)-keyed cache instead of a bespoke
    # yaml.load — the SAME parse then serves son_of_anton_logging's
    # _read_logging_config and any later raw reads in this process, collapsing
    # 3-4 config.yaml parses per invocation into one.
    from son_of_anton_cli.config import read_raw_config as _read_raw_early

    _cfg_path = get_son_of_anton_home() / "config.yaml"
    if _cfg_path.exists():
        _early_cfg_raw = _read_raw_early() or {}
        # Managed scope: overlay administrator-pinned values so a managed
        # security.redact_secrets / network.force_ipv4 wins here too. This early
        # bridge reads config.yaml directly (before load_config is usable), so
        # without the overlay a managed redact_secrets toggle would be ignored.
        # Fail-open via the shared helper.
        try:
            from son_of_anton_cli import managed_scope
            _early_cfg_raw = managed_scope.apply_managed_overlay(_early_cfg_raw)
        except Exception:
            pass
        if "SON_OF_ANTON_REDACT_SECRETS" not in os.environ:
            _early_sec_cfg = _early_cfg_raw.get("security", {})
            if isinstance(_early_sec_cfg, dict):
                _early_redact = _early_sec_cfg.get("redact_secrets")
                if _early_redact is not None:
                    os.environ["SON_OF_ANTON_REDACT_SECRETS"] = str(_early_redact).lower()
        _early_net_cfg = _early_cfg_raw.get("network", {})
        if isinstance(_early_net_cfg, dict) and _early_net_cfg.get("force_ipv4"):
            _FORCE_IPV4_EARLY = True
        del _early_cfg_raw
    del _cfg_path
except Exception:
    pass  # best-effort — redaction stays at default (enabled) on config errors

# Initialize centralized file logging early — all `son-of-anton` subcommands
# (chat, setup, gateway, config, etc.) write to agent.log + errors.log.
try:
    from son_of_anton_logging import setup_logging as _setup_logging

    _setup_logging(mode="cli")
except Exception:
    pass  # best-effort — don't crash the CLI if logging setup fails

# Apply IPv4 preference early, before any HTTP clients are created.
# We already determined whether to force IPv4 from the raw yaml read above —
# this just calls the toggle without a redundant load_config() round trip.
if _FORCE_IPV4_EARLY:
    try:
        from son_of_anton_constants import apply_ipv4_preference as _apply_ipv4

        _apply_ipv4(force=True)
    except Exception:
        pass  # best-effort — don't crash if son_of_anton_constants not importable yet

import logging
import threading
import time as _time
from datetime import datetime

from son_of_anton_cli import __version__, __release_date__

# Provider model-selection wizard flows extracted to son_of_anton_cli/model_setup_flows.py
# (god-file decomposition Phase 2). Re-imported here so select_provider_and_model
# keeps a single dispatch site.
from son_of_anton_cli.model_setup_flows import (
    _model_flow_custom,
    _model_flow_named_custom,
    _model_flow_api_key_provider,
)
logger = logging.getLogger(__name__)


def _read_packed_ref(common_dir: Path, ref: str) -> str | None:
    """Look up a ref in .git/packed-refs without spawning git.

    packed-refs lines look like ``<sha> <ref>`` with optional ``^<sha>``
    peel lines and ``#``-prefixed comments / ``# pack-refs with:`` header.
    """
    try:
        text = (common_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip()
    return None


def _read_git_revision_fingerprint(repo_root: Path) -> str | None:
    """Return a cheap checkout fingerprint without spawning git."""
    git_dir = repo_root / ".git"
    try:
        if git_dir.is_file():
            for line in git_dir.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "gitdir" and value.strip():
                    git_dir = (repo_root / value.strip()).resolve()
                    break
        # Worktrees point HEAD at a per-worktree gitdir but pack their refs
        # in the main repo's gitdir (referenced via ``commondir``). Resolve
        # that up front so packed-refs lookups hit the right file.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            try:
                rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
                if rel:
                    common_dir = (git_dir / rel).resolve()
            except OSError:
                pass
        head_file = git_dir / "HEAD"
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            # Loose refs may live in the worktree gitdir OR the common dir
            # (branches created via `git worktree add` typically live in the
            # common dir's refs/heads/).
            for candidate in (git_dir, common_dir):
                ref_file = candidate / ref
                if ref_file.exists():
                    return f"git:{ref}:{ref_file.read_text(encoding='utf-8', errors='replace').strip()}"
            packed_sha = _read_packed_ref(common_dir, ref)
            if packed_sha:
                return f"git:{ref}:{packed_sha}"
            # Ref name is known but unresolved — still stable across launches,
            # and the version/release fallback in the caller will invalidate
            # after `son-of-anton update`.
            return f"git:{ref}:unresolved"
        return f"git:HEAD:{head}"
    except OSError:
        return None


def _relative_time(ts) -> str:
    """Format a timestamp as relative time (e.g., '2h ago', 'yesterday').

    Thin wrapper kept for backward compatibility; the implementation lives
    in :mod:`son_of_anton_cli.timefmt` so lightweight consumers don't have to
    import the whole CLI surface.
    """
    from son_of_anton_cli.timefmt import relative_time

    return relative_time(ts)


def _has_any_provider_configured() -> bool:
    """Check if at least one inference provider is usable."""
    from son_of_anton_cli.config import get_env_path, get_son_of_anton_home, load_config
    from son_of_anton_cli.auth import get_auth_status

    # Determine whether Son of Anton itself has been explicitly configured (model
    # in config that isn't the hardcoded default). Used below to gate external
    # tool credentials (Claude Code, Codex CLI) that shouldn't silently skip
    # the setup wizard on a fresh install.
    from son_of_anton_cli.config import DEFAULT_CONFIG

    _DEFAULT_MODEL = DEFAULT_CONFIG.get("model", "")
    cfg = load_config()
    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict):
        _default = model_cfg.get("default")
        if isinstance(_default, dict):
            from son_of_anton_cli.config import split_model_config_default
            _model_name, _ = split_model_config_default(_default)
        else:
            _model_name = (_default or "")
        _model_name = (str(_model_name) if not isinstance(_model_name, str) else _model_name).strip()
    elif isinstance(model_cfg, str):
        _model_name = model_cfg.strip()
    else:
        _model_name = ""
    _has_son_of_anton_config = _model_name and _model_name != _DEFAULT_MODEL

    # Check env vars (may be set by .env or shell).
    # OPENAI_BASE_URL alone counts — local models (vLLM, llama.cpp, etc.)
    # often don't require an API key.
    from son_of_anton_cli.auth import PROVIDER_REGISTRY

    # Collect all provider env vars
    provider_env_vars = {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "OPENAI_BASE_URL",
    }
    for pconfig in PROVIDER_REGISTRY.values():
        if pconfig.auth_type == "api_key":
            provider_env_vars.update(pconfig.api_key_env_vars)
    if any(os.getenv(v) for v in provider_env_vars):
        return True

    # Check .env file for keys
    env_file = get_env_path()
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                if key.strip() in provider_env_vars and val:
                    return True
        except Exception:
            pass

    # Cheap local checks first: auth.json and config.yaml are on-disk lookups,
    # while the PROVIDER_REGISTRY sweep below spawns subprocesses (gh) and can
    # take 15-20s — long enough that desktop setup.status calls time out.

    # Check for stored OAuth credentials
    auth_file = get_son_of_anton_home() / "auth.json"
    if auth_file.exists():
        try:
            import json

            auth = json.loads(auth_file.read_text(encoding="utf-8-sig"))
            active = auth.get("active_provider")
            if active:
                status = get_auth_status(active)
                if status.get("logged_in"):
                    return True
        except Exception:
            pass

    # Check config.yaml — if model is a dict with an explicit provider set,
    # the user has gone through setup (fresh installs have model as a plain
    # string).  Also covers custom endpoints that store api_key/base_url in
    # config rather than .env.
    if isinstance(model_cfg, dict):
        cfg_provider = (model_cfg.get("provider") or "").strip()
        cfg_base_url = (model_cfg.get("base_url") or "").strip()
        cfg_api_key = (model_cfg.get("api_key") or "").strip()
        if cfg_provider or cfg_base_url or cfg_api_key:
            return True

    # Check provider-specific auth fallbacks (for example, Copilot via gh auth).
    try:
        for provider_id, pconfig in PROVIDER_REGISTRY.items():
            if pconfig.auth_type != "api_key":
                continue
            status = get_auth_status(provider_id)
            if status.get("logged_in"):
                return True
    except Exception:
        pass

    return False


def _confirm_startup_expensive_model_override(args) -> None:
    """Guard startup -m/--provider overrides before the first API call."""
    explicit_model = (getattr(args, "model", None) or "").strip()
    explicit_provider = (getattr(args, "provider", None) or "").strip()
    if not explicit_model and not explicit_provider:
        return

    try:
        from son_of_anton_cli.config import load_config
        from son_of_anton_cli.model_selection_guards import (
            combined_message,
            selection_warnings,
        )
    except Exception as exc:
        logger.warning("startup model cost guard unavailable: %s", exc)
        return

    try:
        config = load_config()
    except Exception as exc:
        logger.warning("startup model cost guard could not load config: %s", exc)
        config = {}
    if not isinstance(config, dict):
        config = {}
    model_cfg = config.get("model") or {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    security_cfg = config.get("security") or {}
    if not isinstance(security_cfg, dict):
        security_cfg = {}

    model = explicit_model or (model_cfg.get("default") or "").strip()
    if not model:
        return
    provider = (explicit_provider or model_cfg.get("provider") or "").strip()
    try:
        # Unified registry: cost guard + id-keyed guards (e.g. the
        # data-training-tier warning) all fire at startup too.
        warnings = selection_warnings(
            model,
            provider=provider,
            base_url=(model_cfg.get("base_url") or ""),
            api_key=(model_cfg.get("api_key") or ""),
        )
    except Exception as exc:
        logger.warning("startup model cost guard failed for %s/%s: %s", provider, model, exc)
        return
    if not warnings:
        return

    # Cost and provider-routing confirmation is intentionally independent of
    # --yolo / --accept-hooks: those flags approve local command/tool risk, not
    # paid aggregator spend or a surprising provider route.
    is_interactive = sys.stdin.isatty()
    allow_unattended_data_training = (
        security_cfg.get("allow_data_training_tiers_noninteractive") is True
    )
    if not is_interactive and allow_unattended_data_training:
        acknowledged = [
            warning for warning in warnings if warning.kind == "data_policy"
        ]
        if acknowledged:
            sys.stderr.write(combined_message(acknowledged) + "\n")
            sys.stderr.write(
                "Proceeding in non-interactive mode because "
                "security.allow_data_training_tiers_noninteractive is true.\n"
            )
            warnings = [
                warning for warning in warnings if warning.kind != "data_policy"
            ]
            if not warnings:
                return

    message = combined_message(warnings)
    if not is_interactive:
        sys.stderr.write(message + "\n")
        if any(warning.kind == "data_policy" for warning in warnings):
            sys.stderr.write(
                "To acknowledge data-training tiers for unattended runs, set "
                "security.allow_data_training_tiers_noninteractive to true "
                "in config.yaml.\n"
            )
        sys.stderr.write(
            "Refusing this startup model override in non-interactive mode. "
            "Run interactively and confirm if you intend to use it.\n"
        )
        raise SystemExit(1)

    sys.stderr.write(message + "\n")
    try:
        reply = input("Use this model for this invocation? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = ""
    if reply not in {"y", "yes"}:
        sys.stderr.write("Model override cancelled.\n")
        raise SystemExit(1)


def _session_status_tag(status: Optional[str]) -> str:
    """Short fixed-width tag for a session lifecycle status."""
    return {
        "complete": "done",
        "interrupted": "intr",
        "error": "err",
        "empty": "empty",
    }.get(status or "", "-")


def _annotate_session_statuses(sessions: list, session_db) -> None:
    """Attach a ``_status`` key to each session row (best-effort, cheap).

    Uses ``SessionDB.session_lifecycle_statuses`` — one indexed last-message
    lookup per listed session, never a transcript scan. On any failure the
    rows simply stay untagged and the picker renders '-' for status.
    """
    if session_db is None or not sessions:
        return
    try:
        statuses = session_db.session_lifecycle_statuses(
            [s.get("id") for s in sessions]
        )
    except Exception:
        return
    for s in sessions:
        s["_status"] = statuses.get(s.get("id"), "")


def _session_browse_picker(sessions: list, session_db=None) -> Optional[str]:
    """Interactive curses-based session browser with live search filtering.

    Shows lifecycle status (done / intr / err / empty) and message count per
    session when *session_db* is provided. With a live *session_db*, pressing
    ``d`` on a row (while the search filter is empty) prompts y/n and deletes
    the session via ``SessionDB.delete_session``.

    Returns the selected session ID, or None if cancelled.
    """
    if not sessions:
        print("No sessions found.")
        return None

    _annotate_session_statuses(sessions, session_db)

    def _delete_session(session_id: str) -> bool:
        if session_db is None:
            return False
        try:
            sessions_dir = get_son_of_anton_home() / "sessions"
        except Exception:
            sessions_dir = None
        try:
            return bool(
                session_db.delete_session(session_id, sessions_dir=sessions_dir)
            )
        except Exception:
            return False

    # Try curses-based picker first
    try:
        import curses

        result_holder = [None]

        # Layout: [arrow 3] [title/preview flexible] [status 5] [msgs 5]
        #         [active 12] [src 6] [id 18]
        _FIXED_COLS = 3 + 5 + 2 + 5 + 2 + 12 + 6 + 18 + 6

        def _format_row(s, max_x):
            """Format a session row for display."""
            title = (s.get("title") or "").strip()
            preview = (s.get("preview") or "").strip()
            source = s.get("source", "")[:6]
            last_active = _relative_time(s.get("last_active"))
            sid = s["id"][:18]
            status = _session_status_tag(s.get("_status"))
            msgs = s.get("message_count")
            msgs_str = str(msgs) if isinstance(msgs, int) else "-"

            name_width = max(20, max_x - _FIXED_COLS)

            if title:
                name = title[:name_width]
            elif preview:
                name = preview[:name_width]
            else:
                name = sid

            return (
                f"{name:<{name_width}}  {status:<5}  {msgs_str:>5}  "
                f"{last_active:<10}  {source:<5} {sid}"
            )

        def _match(s, query):
            """Check if a session matches the search query (case-insensitive)."""
            q = query.lower()
            return (
                q in (s.get("title") or "").lower()
                or q in (s.get("preview") or "").lower()
                or q in s.get("id", "").lower()
                or q in (s.get("source") or "").lower()
            )

        def _curses_browse(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)  # selected
                curses.init_pair(2, curses.COLOR_YELLOW, -1)  # header
                curses.init_pair(3, curses.COLOR_CYAN, -1)  # search
                curses.init_pair(4, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)  # dim
                curses.init_pair(5, curses.COLOR_RED, -1)  # error/delete

            cursor = 0
            scroll_offset = 0
            search_text = ""
            confirm_delete = None  # session dict pending y/n confirmation
            flash = ""  # one-frame notice (e.g. "deleted <title>")
            filtered = list(sessions)

            def _status_attr(status):
                if not curses.has_colors():
                    return curses.A_NORMAL
                return {
                    "complete": curses.color_pair(1),
                    "interrupted": curses.color_pair(2),
                    "error": curses.color_pair(5),
                    "empty": curses.color_pair(4),
                }.get(status or "", curses.A_NORMAL)

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()
                if max_y < 5 or max_x < 40:
                    # Terminal too small
                    try:
                        stdscr.addstr(0, 0, "Terminal too small")
                    except curses.error:
                        pass
                    stdscr.refresh()
                    stdscr.getch()
                    return

                # Header line
                if search_text:
                    header = f"  Browse sessions — filter: {search_text}█"
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(3)
                else:
                    header = (
                        "  Browse sessions — ↑↓ navigate  Enter select"
                        "  Type to filter  Esc quit"
                    )
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(2)
                try:
                    stdscr.addnstr(0, 0, header, max_x - 1, header_attr)
                except curses.error:
                    pass

                # Column header line
                name_width = max(20, max_x - _FIXED_COLS)
                col_header = (
                    f"   {'Title / Preview':<{name_width}}  {'Stat':<5}  "
                    f"{'Msgs':>5}  {'Active':<10}  {'Src':<5} {'ID'}"
                )
                try:
                    dim_attr = (
                        curses.color_pair(4) if curses.has_colors() else curses.A_DIM
                    )
                    stdscr.addnstr(1, 0, col_header, max_x - 1, dim_attr)
                except curses.error:
                    pass

                # Compute visible area
                visible_rows = max_y - 4  # header + col header + blank + footer
                visible_rows = max(visible_rows, 1)

                # Clamp cursor and scroll
                if not filtered:
                    try:
                        msg = "  No sessions match the filter."
                        stdscr.addnstr(3, 0, msg, max_x - 1, curses.A_DIM)
                    except curses.error:
                        pass
                else:
                    if cursor >= len(filtered):
                        cursor = len(filtered) - 1
                    cursor = max(cursor, 0)
                    if cursor < scroll_offset:
                        scroll_offset = cursor
                    elif cursor >= scroll_offset + visible_rows:
                        scroll_offset = cursor - visible_rows + 1

                    for draw_i, i in enumerate(
                        range(
                            scroll_offset,
                            min(len(filtered), scroll_offset + visible_rows),
                        )
                    ):
                        y = draw_i + 3
                        if y >= max_y - 1:
                            break
                        s = filtered[i]
                        arrow = " → " if i == cursor else "   "
                        row = arrow + _format_row(s, max_x - 3)
                        attr = curses.A_NORMAL
                        if i == cursor:
                            attr = curses.A_BOLD
                            if curses.has_colors():
                                attr |= curses.color_pair(1)
                        try:
                            stdscr.addnstr(y, 0, row, max_x - 1, attr)
                            if i != cursor:
                                # Recolor the status tag column in place.
                                status = s.get("_status")
                                tag = _session_status_tag(status)
                                tag_x = 3 + max(20, (max_x - 3) - _FIXED_COLS) + 2
                                if tag_x + 5 < max_x - 1:
                                    stdscr.addnstr(
                                        y, tag_x, f"{tag:<5}", 5, _status_attr(status)
                                    )
                        except curses.error:
                            pass

                # Footer
                footer_y = max_y - 1
                footer_attr = (
                    curses.color_pair(4) if curses.has_colors() else curses.A_DIM
                )
                if confirm_delete is not None:
                    label = (
                        (confirm_delete.get("title") or "").strip()
                        or (confirm_delete.get("preview") or "").strip()
                        or confirm_delete["id"]
                    )
                    if len(label) > 40:
                        label = label[:37] + "..."
                    footer = f"  Delete session '{label}'? [y/N]"
                    footer_attr = curses.A_BOLD
                    if curses.has_colors():
                        footer_attr |= curses.color_pair(5)
                elif flash:
                    footer = f"  {flash}"
                    flash = ""
                else:
                    if filtered:
                        footer = f"  {cursor + 1}/{len(filtered)} sessions"
                        if len(filtered) < len(sessions):
                            footer += f" (filtered from {len(sessions)})"
                    else:
                        footer = f"  0/{len(sessions)} sessions"
                    if session_db is not None and not search_text:
                        footer += "   d delete"
                try:
                    stdscr.addnstr(footer_y, 0, footer, max_x - 1, footer_attr)
                except curses.error:
                    pass

                stdscr.refresh()
                key = stdscr.getch()

                if confirm_delete is not None:
                    # y/n confirmation mode — only an explicit 'y' deletes.
                    target = confirm_delete
                    confirm_delete = None
                    if key in {ord("y"), ord("Y")}:
                        if _delete_session(target["id"]):
                            sessions[:] = [
                                s for s in sessions if s["id"] != target["id"]
                            ]
                            filtered = (
                                [s for s in sessions if _match(s, search_text)]
                                if search_text
                                else list(sessions)
                            )
                            flash = "Deleted."
                            if not sessions:
                                return
                        else:
                            flash = "Delete failed."
                    continue

                if key in {curses.KEY_UP,}:
                    if filtered:
                        cursor = (cursor - 1) % len(filtered)
                elif key in {curses.KEY_DOWN,}:
                    if filtered:
                        cursor = (cursor + 1) % len(filtered)
                elif key in {curses.KEY_ENTER, 10, 13}:
                    if filtered:
                        result_holder[0] = filtered[cursor]["id"]
                    return
                elif key == 27:  # Esc
                    if search_text:
                        # First Esc clears the search
                        search_text = ""
                        filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                    else:
                        # Second Esc exits
                        return
                elif key in {curses.KEY_BACKSPACE, 127, 8}:
                    if search_text:
                        search_text = search_text[:-1]
                        if search_text:
                            filtered = [s for s in sessions if _match(s, search_text)]
                        else:
                            filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                elif key == ord("q") and not search_text:
                    return
                elif (
                    key == ord("d")
                    and not search_text
                    and session_db is not None
                    and filtered
                ):
                    # 'd' only acts as delete when the filter is empty —
                    # while a search is active it types into the query below.
                    confirm_delete = filtered[cursor]
                elif 32 <= key <= 126:
                    # Printable character → add to search filter
                    search_text += chr(key)
                    filtered = [s for s in sessions if _match(s, search_text)]
                    cursor = 0
                    scroll_offset = 0

        curses.wrapper(_curses_browse)
        return result_holder[0]

    except Exception:
        pass

    # Fallback: numbered list when the interactive picker can't run. Shows
    # the same status/message-count columns but has no delete support.
    print("\n  Browse sessions  (enter number to resume, q to cancel)\n")
    for i, s in enumerate(sessions):
        title = (s.get("title") or "").strip()
        preview = (s.get("preview") or "").strip()
        label = title or preview or s["id"]
        if len(label) > 50:
            label = label[:47] + "..."
        last_active = _relative_time(s.get("last_active"))
        src = s.get("source", "")[:6]
        status = _session_status_tag(s.get("_status"))
        msgs = s.get("message_count")
        msgs_str = str(msgs) if isinstance(msgs, int) else "-"
        print(
            f"  {i + 1:>3}. {label:<50}  {status:<5}  {msgs_str:>5}  "
            f"{last_active:<10}  {src}"
        )

    while True:
        try:
            val = input(f"\n  Select [1-{len(sessions)}]: ").strip()
            if not val or val.lower() in {"q", "quit", "exit"}:
                return None
            idx = int(val) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]["id"]
            print(f"  Invalid selection. Enter 1-{len(sessions)} or q to cancel.")
        except ValueError:
            print("  Invalid input. Enter a number or q to cancel.")
        except (KeyboardInterrupt, EOFError):
            print()
            return None


def _resolve_workspace_key() -> Optional[str]:
    """The current workspace identity for cwd-scoped resume.

    Git repo root when CWD is inside a repo (so all sessions across its
    subdirs/worktrees group together), else the CWD itself. Returns None when
    neither can be determined — callers fall back to the global MRU then.
    """
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return os.path.abspath(result.stdout.strip())
    except Exception:
        pass
    try:
        return os.getcwd()
    except Exception:
        return None


def _resolve_last_session(source: str = "cli") -> Optional[str]:
    """Look up the most recently-used session ID for a source.

    Scoped to the current workspace first (git repo root, else cwd) so
    ``son-of-anton -c`` from repo A continues repo A's last session rather than the
    global MRU. Falls back to the unscoped MRU when no session matches the
    current workspace, preserving the old behaviour for fresh directories.
    """
    db = None
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()
        ws_key = _resolve_workspace_key()
        if ws_key:
            sessions = db.search_sessions(source=source, limit=1, workspace_key=ws_key)
            if sessions:
                return sessions[0]["id"]
        # Fallback: global MRU for this source.
        sessions = db.search_sessions(source=source, limit=1)
        return sessions[0]["id"] if sessions else None
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return None


def _resolve_session_by_name_or_id(name_or_id: str) -> Optional[str]:
    """Resolve a session name (title) or ID to a session ID.

    - If it looks like a session ID (contains underscore + hex), try direct lookup first.
    - Otherwise, treat it as a title and use resolve_session_by_title (auto-latest).
    - Falls back to the other method if the first doesn't match.
    - If the resolved session is a compression root, follow the chain forward
      to the latest continuation. Users who remember the old root ID (e.g.
      from an exit summary printed before the bug fix, or from notes) get
      resumed at the live tip instead of a stale parent with no messages.
    """
    db = None
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()

        # Try as exact session ID first
        session = db.get_session(name_or_id)
        resolved_id: Optional[str] = None
        if session:
            resolved_id = session["id"]
        else:
            # Try as title (with auto-latest for lineage)
            resolved_id = db.resolve_session_by_title(name_or_id)

        if resolved_id:
            # Project forward through compression chain so resumes land on
            # the live tip instead of a dead compressed parent.
            try:
                resolved_id = db.get_compression_tip(resolved_id) or resolved_id
            except Exception:
                pass

        return resolved_id
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return None


def _create_titled_session(title: str) -> Optional[str]:
    """Create a fresh session with the given title; return its session id.

    Used by ``chat -c <title> --create-if-missing`` (#86794): programmatic
    callers (plugins, scripts) that want "send to this named thread, making
    it if needed" get a deterministic outcome instead of a silent no-op.

    The session id follows the same timestamp+uuid shape the CLI uses for a
    brand-new session; the title is recorded with user provenance so
    auto-titling never overwrites it.
    """
    db = None
    try:
        import uuid as _uuid

        from son_of_anton_state import SessionDB

        now = datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        short_uuid = _uuid.uuid4().hex[:6]
        new_session_id = f"{timestamp_str}_{short_uuid}"

        db = SessionDB()
        db.create_session(new_session_id, source="cli")
        db.set_session_title(new_session_id, title)
        return new_session_id
    except Exception:
        # Programmatic callers (the #86794 use case) rely on --create-if-missing
        # being deterministic; swallow the failure to keep the error path simple,
        # but log the underlying cause so it lands in errors.log and stays
        # debuggable (DB lock, I/O error, import error — all otherwise invisible).
        logger.exception("Failed to create titled session %r", title)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _resolve_continue_arg(args) -> None:
    """Resolve ``-c/--continue`` into ``args.resume``.

    Handles both forms:
    - ``-c <name>``: resolve by title/ID. On miss, fail loudly on **stderr**
      (exit 1) so programmatic callers see the error even under quiet mode
      (#86794); with ``--create-if-missing``, create a fresh titled session
      and resume into it instead.
    - bare ``-c``: continue this terminal's breadcrumb session if valid,
      else the most recent session (workspace-scoped MRU, then global
      fallback).
    """
    continue_val = getattr(args, "continue_last", None)
    if continue_val and not getattr(args, "resume", None):
        if isinstance(continue_val, str):
            # -c "session name" — resolve by title or ID
            resolved = _resolve_session_by_name_or_id(continue_val)
            if resolved:
                args.resume = resolved
            elif getattr(args, "create_if_missing", False):
                # --create-if-missing: no session matches the title — create a
                # new session with that title and proceed. This is the
                # programmatic-caller primitive ("send to this named thread,
                # making it if needed"); without it a background/quiet send to
                # a not-yet-existing named session silently no-ops (#86794).
                new_sid = _create_titled_session(continue_val)
                if new_sid:
                    args.resume = new_sid
                else:
                    print(
                        f"No session found matching '{continue_val}' and "
                        "a new titled session could not be created.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                print(f"No session found matching '{continue_val}'.", file=sys.stderr)
                print(
                    "Use 'son-of-anton sessions list' to see available sessions, or "
                    "pass --create-if-missing to start a new session with that title.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            # -c with no argument — prefer this terminal's own breadcrumb
            # (written at session start / rotation) so side-by-side terminals
            # each continue their own conversation. Falls back to the
            # most-recent session when there is no valid breadcrumb, or when
            # session.terminal_continue is false in config.yaml.
            if getattr(args, "create_if_missing", False):
                # --create-if-missing only makes sense with a named session;
                # with a bare -c there is nothing to create, so surface the
                # no-op to programmatic callers instead of silently ignoring it.
                print(
                    "--create-if-missing requires a session name: "
                    "`-c <name> --create-if-missing`",
                    file=sys.stderr,
                )
            try:
                from son_of_anton_cli.terminal_breadcrumbs import resolve_breadcrumb_session

                _crumb_id = resolve_breadcrumb_session()
            except Exception:
                _crumb_id = None
            if _crumb_id:
                args.resume = _crumb_id
            else:
                # No valid breadcrumb — continue the most recent session
                last_id = _resolve_last_session(source="cli")
                if last_id:
                    args.resume = last_id
                else:
                    print("No previous CLI session found to continue.")
                    sys.exit(1)


def cmd_chat(args):
    """Run interactive chat CLI."""
    _apply_safe_mode(args)

    # --in DIR: run in DIR. Must happen before any session resolution so the
    # workspace-scoped "latest"/-c lookups key off DIR, and it pins the
    # session there — an explicit --in wins over a resumed session's
    # recorded cwd (so the restore step below is skipped).
    in_dir = getattr(args, "in_dir", None)
    if in_dir:
        _target_dir = os.path.abspath(os.path.expanduser(in_dir))
        if not os.path.isdir(_target_dir):
            print(f"Error: --in directory not found: {in_dir}")
            sys.exit(1)
        try:
            os.chdir(_target_dir)
        except OSError as e:
            print(f"Error: cannot enter --in directory {in_dir}: {e}")
            sys.exit(1)
        args.no_restore_cwd = True

    # --resume latest: keyword for "most recent session" — same resolution
    # as `-c` with no name (workspace-scoped MRU, then global fallback).
    # The keyword wins over a session literally titled "latest"; that
    # session stays reachable via its ID or `-c latest` (title match).
    _resume_raw = getattr(args, "resume", None)
    if isinstance(_resume_raw, str) and _resume_raw.strip().lower() == "latest":
        _last_id = _resolve_last_session(source="cli")
        if _last_id:
            args.resume = _last_id
        else:
            print("No previous CLI session found to resume.")
            print("Use 'son-of-anton sessions list' to see available sessions.")
            sys.exit(1)

    # Resolve --continue into --resume with the latest session or by name
    _resolve_continue_arg(args)

    # --resume @claude / --resume @codex: import a foreign session (Claude
    # Code / Codex CLI) and resume the newly created Son of Anton session.
    _resume_foreign = getattr(args, "resume", None)
    if isinstance(_resume_foreign, str) and _resume_foreign.strip().lower() in (
        "@claude",
        "@codex",
    ):
        from son_of_anton_cli.foreign_sessions import (
            import_foreign_session,
            pick_foreign_session,
        )

        _foreign_source = _resume_foreign.strip().lower().lstrip("@")
        _picked = pick_foreign_session(_foreign_source)
        if _picked is None:
            sys.exit(1)
        try:
            _imported_id = import_foreign_session(_picked.source, _picked.path)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        print(f"✓ Imported as {_imported_id} — resuming it now.")
        print(f"  (later: son-of-anton --resume {_imported_id})")
        args.resume = _imported_id

    # Resolve --resume by title if it's not a direct session ID
    resume_val = getattr(args, "resume", None)
    if resume_val:
        resolved = _resolve_session_by_name_or_id(resume_val)
        if resolved:
            args.resume = resolved
        # If resolution fails, keep the original value — _init_agent will
        # report "Session not found" with the original input

    # Session<->workspace binding: cd back into a resumed session's recorded cwd
    # so it resumes in the repo it belonged to. Opt out with --no-restore-cwd;
    # skipped under --worktree (that path owns its own dir). Best-effort — a
    # missing dir warns and stays put rather than failing the resume.
    if (
        getattr(args, "resume", None)
        and not getattr(args, "no_restore_cwd", False)
        and not getattr(args, "worktree", False)
    ):
        _resume_db = None
        try:
            from son_of_anton_state import SessionDB

            _resume_db = SessionDB()
            _saved_cwd = ((_resume_db.get_session(args.resume) or {}).get("cwd") or "").strip()
            if _saved_cwd and not os.path.isdir(_saved_cwd):
                print(f"⚠ session's recorded dir is gone ({_saved_cwd}); staying in {os.getcwd()}")
            elif _saved_cwd and os.path.realpath(_saved_cwd) != os.path.realpath(os.getcwd()):
                os.chdir(_saved_cwd)
                print(f"↪ restored workspace dir: {_saved_cwd}")
        except Exception:
            pass  # never let cwd-restore break a resume
        finally:
            if _resume_db is not None:
                try:
                    _resume_db.close()
                except Exception:
                    pass

    # First-run guard: check if any provider is configured before launching
    if not _has_any_provider_configured():
        print()
        print(
            "It looks like Son of Anton isn't configured yet -- no API keys or providers found."
        )
        print()
        print("  Run:  son-of-anton setup")
        print()

        from son_of_anton_cli.setup import (
            is_interactive_stdin,
            print_noninteractive_setup_guidance,
        )

        if not is_interactive_stdin():
            print_noninteractive_setup_guidance(
                "No interactive TTY detected for the first-run setup prompt."
            )
            sys.exit(1)

        try:
            reply = input("Run setup now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            reply = "n"
        if reply in {"", "y", "yes"}:
            cmd_setup(args)
            return
        print()
        print("You can run 'son-of-anton setup' at any time to configure.")
        sys.exit(1)

    # Start update check in background (runs while other init happens).
    try:
        from son_of_anton_cli.banner import prefetch_banner_data, prefetch_update_check

        prefetch_update_check()
        # Warm git banner state + skills index off-thread too — their
        # subprocess/file-I/O waits overlap the CPU-bound cli import.
        prefetch_banner_data()
    except Exception:
        pass

    # Sync bundled skills on every CLI launch. Normally runs in a background
    # daemon thread: the sync is idempotent, hash-gated (unchanged skills are
    # skipped), and nothing on the banner path depends on it, yet the scan
    # alone costs ~120-170ms of rglob/hashing on the startup path. Skill
    # loading happens at agent init (first message), by which point the
    # sync has long finished.
    #
    # FIRST RUN is the exception: with an empty ~/.son-of-anton/skills the banner
    # prefetch races the background sync, caches an empty skills index, and
    # the very first launch greets the user with "No skills installed ·
    # 0 skills" while 69 bundled skills land milliseconds later (full-surface
    # CLI QA sweep, Aug 2026). Run the sync in the foreground exactly once —
    # only when the skills dir has no SKILL.md yet — so the first impression
    # matches reality; every later launch keeps the background path.
    def _skills_dir_is_unseeded() -> bool:
        try:
            from son_of_anton_cli.config import get_son_of_anton_home
            skills_dir = Path(get_son_of_anton_home()) / "skills"
            if not skills_dir.is_dir():
                return True
            return next(skills_dir.rglob("SKILL.md"), None) is None
        except Exception:
            return False

    def _skills_sync_bg() -> None:
        try:
            from tools.skills_sync import sync_skills

            sync_skills(quiet=True)
        except Exception:
            pass

    if _skills_dir_is_unseeded():
        _skills_sync_bg()
        # The banner prefetch thread (started above) may have scanned the
        # still-empty dir and cached an empty skills index — drop it so the
        # banner recomputes against the freshly seeded tree.
        try:
            import son_of_anton_cli.banner as _banner_mod
            _banner_mod._available_skills_cache = None
        except Exception:
            pass
    else:
        threading.Thread(
            target=_skills_sync_bg, name="bundled-skills-sync", daemon=True
        ).start()

    # --yolo: bypass all dangerous command approvals.
    # Also set in main() before _prepare_agent_startup() — that is the
    # authoritative site because it runs before tool imports freeze
    # _YOLO_MODE_FROZEN.  This redundant set is a safety net for callers
    # that invoke cmd_chat directly (e.g. subcommand dispatch).
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"

    # --ignore-user-config: make load_cli_config() / load_config() skip the
    # user's ~/.son-of-anton/config.yaml and return built-in defaults. Set BEFORE
    # importing cli (which runs `CLI_CONFIG = load_cli_config()` at module
    # import time). Credentials in .env are still loaded — this flag only
    # ignores behavioral/config settings.
    if getattr(args, "ignore_user_config", False):
        os.environ["SON_OF_ANTON_IGNORE_USER_CONFIG"] = "1"

    # --ignore-rules: skip auto-injection of AGENTS.md/SOUL.md/.cursorrules
    # (rules), memory entries, and any preloaded skills coming from user config.
    # Maps to AIAgent(skip_context_files=True, skip_memory=True).
    if getattr(args, "ignore_rules", False):
        os.environ["SON_OF_ANTON_IGNORE_RULES"] = "1"

    # --source: tag session source for filtering (e.g. 'tool' for third-party integrations)
    if getattr(args, "source", None):
        os.environ["SON_OF_ANTON_SESSION_SOURCE"] = args.source

    _confirm_startup_expensive_model_override(args)

    # Import and run the CLI
    from cli import main as cli_main

    # --query-file: read the single query from a file (or stdin via '-') so
    # callers never have to shell-quote message bodies. This is the transport
    # the Bot Mode DM protocol uses — interpolating arbitrary text into a
    # double-quoted shell argument truncates on quotes and executes $(...)
    # (see tools/bot_mode_probe.py).
    _qfile = getattr(args, "query_file", None)
    if _qfile:
        if args.query:
            # argparse's mutually-exclusive group catches the normal CLI path;
            # this guards programmatic callers that fill the namespace directly.
            print("Error: -q/--query and --query-file are mutually exclusive", file=sys.stderr)
            sys.exit(2)
        try:
            if _qfile == "-":
                args.query = sys.stdin.read()
            else:
                with open(_qfile, "r", encoding="utf-8", errors="replace") as _fh:
                    args.query = _fh.read()
        except OSError as _e:
            print(f"Error: cannot read --query-file {_qfile}: {_e}", file=sys.stderr)
            sys.exit(2)
        if not (args.query or "").strip():
            print(f"Error: --query-file {_qfile} is empty", file=sys.stderr)
            sys.exit(2)

    # Build kwargs from args
    kwargs = {
        "model": args.model,
        "provider": getattr(args, "provider", None),
        "reasoning": getattr(args, "reasoning", None),
        "toolsets": args.toolsets,
        "skills": getattr(args, "skills", None),
        "verbose": getattr(args, "verbose", None),
        "quiet": getattr(args, "quiet", False),
        "query": args.query,
        "image": getattr(args, "image", None),
        "resume": getattr(args, "resume", None),
        "worktree": getattr(args, "worktree", False),
        "checkpoints": getattr(args, "checkpoints", False),
        "pass_session_id": getattr(args, "pass_session_id", False),
        "max_turns": getattr(args, "max_turns", None),
        "run_budget": getattr(args, "run_budget", None),
        "ignore_rules": getattr(args, "ignore_rules", False) or getattr(args, "safe_mode", False),
        "ignore_user_config": getattr(args, "ignore_user_config", False) or getattr(args, "safe_mode", False),
        "compact": getattr(args, "compact", False),
    }
    # Filter out None values
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    try:
        cli_main(**kwargs)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def _sync_bundled_skills_quietly() -> None:
    """Seed ``~/.son-of-anton/skills/`` with the bundled skill library on first launch.

    Called from any CLI entrypoint that the user might use as their first
    interaction with Son of Anton — chat and gateway. The skills_sync module is
    manifest-based and idempotent: skipped skills cost ~milliseconds, so
    calling this repeatedly is fine.

    Failures are swallowed because skills are an enhancement, not a hard
    dependency. Son of Anton still functions without them; the user just sees an
    empty skills library.
    """
    try:
        from tools.skills_sync import sync_skills

        sync_skills(quiet=True)
    except Exception:
        pass


def cmd_gateway(args):
    """Gateway management commands."""
    _sync_bundled_skills_quietly()

    from son_of_anton_cli.gateway import gateway_command

    gateway_command(args)


def cmd_model(args):
    """Select default model — starts with provider selection, then model picker."""
    _require_tty("model")
    if getattr(args, "refresh", False):
        try:
            from son_of_anton_cli.models import clear_provider_models_cache
            clear_provider_models_cache()
            print("  Cleared model picker cache.")
        except Exception:
            pass
    select_provider_and_model(args=args)


def _is_profile_api_key_provider(provider_id: str) -> bool:
    """Return True when provider_id maps to a profile with auth_type='api_key'.

    Used as a catch-all in select_provider_and_model() so that new providers
    declared in plugins/model-providers/<name>/ automatically dispatch to _model_flow_api_key_provider
    without requiring an explicit elif branch here.
    """
    try:
        from providers import get_provider_profile
        _p = get_provider_profile(provider_id)
        return _p is not None and _p.auth_type == "api_key"
    except Exception:
        return False


def select_provider_and_model(args=None):
    """Core provider selection + model picking logic.

    Shared by ``cmd_model`` (``son-of-anton model``) and the setup wizard
    (``setup_model_provider`` in setup.py).  Handles the full flow:
    provider picker, credential prompting, model selection, and config
    persistence.
    """
    from son_of_anton_cli.auth import (
        resolve_provider,
        AuthError,
        format_auth_error,
    )
    from son_of_anton_cli.config import (
        get_compatible_custom_providers,
        load_config,
        get_env_value,
    )
    from son_of_anton_cli.providers import (
        custom_provider_aliases,
        custom_provider_slug,
        resolve_provider_full,
    )

    config = load_config()
    current_model = config.get("model")
    if isinstance(current_model, dict):
        current_model = current_model.get("default", "")
    current_model = current_model or "(not set)"

    # Read effective provider the same way the CLI does at startup:
    # config.yaml model.provider > env var > auto-detect
    config_provider = None
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        config_provider = model_cfg.get("provider")

    effective_provider = (
        config_provider or os.getenv("SON_OF_ANTON_INFERENCE_PROVIDER") or "auto"
    )
    compatible_custom_providers = get_compatible_custom_providers(config)
    def _named_custom_provider_map(cfg) -> dict[str, dict[str, str]]:
        from son_of_anton_cli.config import read_raw_config

        # Build lookups of raw (un-expanded) templates keyed by a
        # stable identity. We intentionally bypass
        # ``get_compatible_custom_providers(read_raw_config())`` here because
        # its ``_normalize_custom_provider_entry`` step calls ``urlparse()``
        # on ``base_url`` and drops any entry whose ``base_url`` is itself an
        # env-ref template (e.g. ``${NEURALWATT_API_BASE}``). Dropping those
        # entries is exactly how env-ref preservation fails for the user
        # config that motivated this fix.
        raw_api_key_refs: dict[tuple, str] = {}
        raw_base_url_refs: dict[tuple, str] = {}
        raw_cfg = read_raw_config()

        def _record_raw(
            name: str,
            provider_key: str,
            model: str,
            api_key: str,
            base_url: str,
        ) -> None:
            template = str(api_key or "").strip()
            base_template = str(base_url or "").strip()
            name = str(name or "").strip()
            provider_key = str(provider_key or "").strip()
            model = str(model or "").strip()
            # Index by every plausible identity the loaded (expanded) config
            # might present: (name), (name, model), (provider_key), and
            # (provider_key, model). Case-insensitive on name/provider_key so
            # the loaded entry matches regardless of display casing.
            identities = []
            if name:
                identities.extend(((name.lower(),), (name.lower(), model)))
            if provider_key:
                identities.extend(
                    ((provider_key.lower(),), (provider_key.lower(), model))
                )
            if "${" in template:
                for identity in identities:
                    raw_api_key_refs.setdefault(identity, template)
            if "${" in base_template:
                for identity in identities:
                    raw_base_url_refs.setdefault(identity, base_template)

        raw_list = raw_cfg.get("custom_providers")
        if isinstance(raw_list, list):
            for raw_entry in raw_list:
                if not isinstance(raw_entry, dict):
                    continue
                _record_raw(
                    raw_entry.get("name", ""),
                    "",
                    raw_entry.get("model", "") or raw_entry.get("default_model", ""),
                    raw_entry.get("api_key", ""),
                    raw_entry.get("base_url", "")
                    or raw_entry.get("url", "")
                    or raw_entry.get("api", ""),
                )
        raw_providers = raw_cfg.get("providers")
        if isinstance(raw_providers, dict):
            for raw_key, raw_entry in raw_providers.items():
                if not isinstance(raw_entry, dict):
                    continue
                _record_raw(
                    raw_entry.get("name", "") or raw_key,
                    raw_key,
                    raw_entry.get("model", "") or raw_entry.get("default_model", ""),
                    raw_entry.get("api_key", ""),
                    raw_entry.get("base_url", "")
                    or raw_entry.get("url", "")
                    or raw_entry.get("api", ""),
                )

        def _lookup_ref(
            refs: dict[tuple, str],
            name: str,
            provider_key: str,
            model: str,
        ) -> str:
            name_lc = str(name or "").strip().lower()
            pkey_lc = str(provider_key or "").strip().lower()
            model = str(model or "").strip()
            for identity in (
                (pkey_lc, model),
                (pkey_lc,),
                (name_lc, model),
                (name_lc,),
            ):
                if identity[0] and identity in refs:
                    return refs[identity]
            return ""

        custom_provider_map = {}
        for entry in get_compatible_custom_providers(cfg):
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            base_url = (entry.get("base_url") or "").strip()
            if not name or not base_url:
                continue
            provider_key = (entry.get("provider_key") or "").strip()
            key = custom_provider_slug(name, provider_key)
            custom_provider_map[key] = {
                "name": name,
                "base_url": base_url,
                "api_key": entry.get("api_key", ""),
                "key_env": entry.get("key_env") or entry.get("api_key_env", ""),
                "model": entry.get("model", ""),
                "models": entry.get("models", {}),
                "models_discovered": entry.get("models_discovered", False),
                "extra_headers": entry.get("extra_headers", {}),
                "discover_models": entry.get("discover_models", True),
                "api_mode": entry.get("api_mode", ""),
                "provider_key": provider_key,
                "api_key_ref": _lookup_ref(
                    raw_api_key_refs, name, provider_key, entry.get("model", "")
                ),
                "base_url_ref": _lookup_ref(
                    raw_base_url_refs, name, provider_key, entry.get("model", "")
                ),
            }
        return custom_provider_map

    def _norm_base_url(url: str) -> str:
        return str(url or "").strip().rstrip("/").lower()

    # Add user-defined custom providers from config.yaml
    _custom_provider_map = _named_custom_provider_map(
        config
    )  # key → {name, base_url, api_key}

    def _canonical_named_custom_key(provider_id: str) -> str:
        requested = str(provider_id or "").strip().lower()
        for key, provider_info in _custom_provider_map.items():
            if requested in custom_provider_aliases(
                provider_info.get("name", ""),
                provider_info.get("provider_key", ""),
            ):
                return key
        return provider_id

    def _active_custom_key_from_base_url() -> str:
        if effective_provider != "custom" or not isinstance(model_cfg, dict):
            return ""
        current_base = _norm_base_url(model_cfg.get("base_url", ""))
        if not current_base:
            return ""
        for key, provider_info in _custom_provider_map.items():
            if _norm_base_url(provider_info.get("base_url", "")) == current_base:
                return key
        return ""

    active = _active_custom_key_from_base_url()
    if active is None:
        active = ""
    if not active and effective_provider != "auto":
        active_def = resolve_provider_full(
            effective_provider,
            config.get("providers"),
            compatible_custom_providers,
        )
        if active_def is not None:
            active = active_def.id
            if active_def.source == "user-config":
                active = _canonical_named_custom_key(active)
        else:
            warning = (
                f"Unknown provider '{effective_provider}'. Check 'son-of-anton model' for "
                "available providers, or run 'son-of-anton doctor' to diagnose config "
                "issues."
            )
            print(f"Warning: {warning} Falling back to auto provider detection.")
    if not active:
        try:
            active = resolve_provider("auto")
        except AuthError as exc:
            if effective_provider == "auto":
                warning = format_auth_error(exc)
                print(f"Warning: {warning} Falling back to auto provider detection.")
            active = None  # no provider yet; default to first in list

    # Detect custom endpoint
    from son_of_anton_cli.models import (
        CANONICAL_PROVIDERS,
        _PROVIDER_LABELS,
        _PROVIDER_ALIASES,
        group_providers,
        provider_group_for_slug,
    )

    provider_labels = dict(_PROVIDER_LABELS)  # derive from canonical list
    if active and active in _custom_provider_map:
        active_label = _custom_provider_map[active]["name"]
    else:
        active_label = provider_labels.get(active, active) if active else "none"

    print()
    print(f"  Current model:    {current_model}")
    print(f"  Active provider:  {active_label}")
    print()

    # Step 1: Provider selection.
    #
    # Canonical providers are folded into top-level groups (display only — see
    # PROVIDER_GROUPS in son_of_anton_cli/models.py). A multi-member group shows one
    # row ("Kimi / Moonshot ▸"); picking it opens a member sub-picker that
    # resolves back to a concrete slug, so the dispatch chain below is
    # unchanged. Custom providers and the trailing actions stay flat.
    canonical_descs = {p.slug: p.tui_desc for p in CANONICAL_PROVIDERS}
    # Honor ``model_catalog.excluded_providers`` so the CLI ``son-of-anton model``
    # picker hides the same providers the gateway/TUI pickers do. A canonical
    # provider is hidden if its slug OR any of its aliases appears in the
    # exclusion list (case-insensitive), matching list_authenticated_providers'
    # matching against son_of_anton_id / alias / canonical slug.
    _cli_excluded = {
        str(p).strip().lower()
        for p in (config.get("model_catalog", {}) or {}).get("excluded_providers") or []
        if p
    }
    if _cli_excluded:
        _alias_to_canon = _PROVIDER_ALIASES
        _names_for: dict[str, set[str]] = {}
        for _p in CANONICAL_PROVIDERS:
            _names_for[_p.slug] = {_p.slug.lower()}
        for _alias, _canon in _alias_to_canon.items():
            _names_for.setdefault(_canon, {_canon.lower()}).add(_alias.lower())
        _visible_slugs = [
            p.slug for p in CANONICAL_PROVIDERS
            if not _names_for.get(p.slug, {p.slug.lower()}) & _cli_excluded
        ]
    else:
        _visible_slugs = [p.slug for p in CANONICAL_PROVIDERS]
    grouped_rows = group_providers(_visible_slugs)

    # The group/slug that should be pre-selected: the active provider's group
    # if it's grouped, otherwise the active slug itself.
    active_group = provider_group_for_slug(active) if active else ""

    # ordered entries: (key, label, members)
    #   members == [] → leaf row, key is a provider slug / action
    #   members != [] → group row, key is "group:<gid>"
    ordered: list[tuple[str, str, list[str]]] = []
    default_idx = 0
    for row in grouped_rows:
        if row["kind"] == "group":
            gid = row["group_id"]
            group_desc = row.get("description", "")
            label = f"{row['label']} ▸ ({group_desc})" if group_desc else f"{row['label']} ▸"
            key = f"group:{gid}"
            is_active = bool(active_group) and gid == active_group
            members = row["members"]
        else:
            slug = row["slug"]
            label = canonical_descs.get(slug, provider_labels.get(slug, slug))
            key = slug
            is_active = bool(active) and slug == active
            members = []
        if is_active:
            ordered.append((key, f"{label}  ← currently active", members))
            default_idx = len(ordered) - 1
        else:
            ordered.append((key, label, members))

    for key, provider_info in _custom_provider_map.items():
        name = provider_info["name"]
        base_url = provider_info["base_url"]
        short_url = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        saved_model = provider_info.get("model", "")
        model_hint = f" — {saved_model}" if saved_model else ""
        label = f"{name} ({short_url}){model_hint}"
        if active and key == active:
            ordered.append((key, f"{label}  ← currently active", []))
            default_idx = len(ordered) - 1
        else:
            ordered.append((key, label, []))

    ordered.append(("custom", "Custom endpoint (enter URL manually)", []))
    _has_saved_custom_list = isinstance(config.get("custom_providers"), list) and bool(
        config.get("custom_providers")
    )
    if _has_saved_custom_list:
        ordered.append(("remove-custom", "Remove a saved custom provider", []))
    ordered.append(("aux-config", "Configure auxiliary models...", []))
    ordered.append(("cancel", "Leave unchanged", []))

    provider_idx = _prompt_provider_choice(
        [label for _, label, _ in ordered],
        default=default_idx,
    )
    if provider_idx is None or ordered[provider_idx][0] == "cancel":
        print("No change.")
        return

    selected_key = ordered[provider_idx][0]
    selected_members = ordered[provider_idx][2]

    # Group row → drill into a member sub-picker. Default to the active member
    # if the active provider lives in this group. The descriptive text lives on
    # the group row itself, so member rows show only their short label here.
    if selected_members:
        member_default = 0
        if active in selected_members:
            member_default = selected_members.index(active)
        member_labels = [
            provider_labels.get(m, m) for m in selected_members
        ]
        group_label = ordered[provider_idx][1].split(" ▸", 1)[0]
        member_idx = _prompt_provider_choice(
            member_labels,
            default=member_default,
            title=f"Select {group_label} provider:",
        )
        if member_idx is None:
            print("No change.")
            return
        selected_provider = selected_members[member_idx]
    else:
        selected_provider = selected_key

    if selected_provider == "aux-config":
        _aux_config_menu()
        return

    # Step 2: Provider-specific setup + model selection
    if selected_provider == "custom":
        _model_flow_custom(config)
    elif (
        selected_provider.startswith("custom:")
        or selected_provider in _custom_provider_map
    ):
        provider_info = _named_custom_provider_map(load_config()).get(selected_provider)
        if provider_info is None:
            print(
                "Warning: the selected saved custom provider is no longer available. "
                "It may have been removed from config.yaml. No change."
            )
            return
        _model_flow_named_custom(config, provider_info)
    elif selected_provider == "remove-custom":
        _remove_custom_provider(config)
    elif selected_provider == "openai-api" or _is_profile_api_key_provider(
        selected_provider
    ):
        _model_flow_api_key_provider(config, selected_provider, current_model)

    # ── Post-switch cleanup: clear stale OPENAI_BASE_URL ──────────────
    # When the user switches to a named provider (anything except "custom"),
    # a leftover OPENAI_BASE_URL in ~/.son-of-anton/.env can poison auxiliary
    # clients that use provider:auto. Clear it proactively.  (#5161)
    if selected_provider not in {
        "custom",
        "cancel",
        "remove-custom",
    } and not selected_provider.startswith("custom:"):
        _clear_stale_openai_base_url()


def _clear_stale_openai_base_url():
    """Remove OPENAI_BASE_URL from ~/.son-of-anton/.env if the active provider is not 'custom'.

    After a provider switch, a leftover OPENAI_BASE_URL causes auxiliary
    clients (compression, vision, delegation) with provider:auto to route
    requests to the old custom endpoint instead of the newly selected
    provider.  See issue #5161.
    """
    from son_of_anton_cli.config import get_env_value, save_env_value, load_config

    cfg = load_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        provider = (model_cfg.get("provider") or "").strip().lower()
    else:
        provider = ""

    if provider == "custom" or not provider:
        return  # custom provider legitimately uses OPENAI_BASE_URL

    stale_url = get_env_value("OPENAI_BASE_URL")
    if stale_url:
        save_env_value("OPENAI_BASE_URL", "")
        print(
            f"Cleared stale OPENAI_BASE_URL from .env (was: {stale_url[:40]}...)"
            if len(stale_url) > 40
            else f"Cleared stale OPENAI_BASE_URL from .env (was: {stale_url})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Auxiliary model configuration
#
# Son of Anton uses lightweight "auxiliary" models for side tasks (vision analysis,
# context compression, web extraction, session search, etc.). Each task has
# its own provider+model pair in config.yaml under `auxiliary.<task>`.
#
# The UI lives behind "Configure auxiliary models..." at the bottom of the
# `son-of-anton model` provider picker. It does NOT re-run credential setup — it
# only routes already-authenticated providers to specific aux tasks. Users
# configure new providers through the normal `son-of-anton model` flow first.
# ─────────────────────────────────────────────────────────────────────────────

# (task_key, display_name, short_description)
_AUX_TASKS: list[tuple[str, str, str]] = [
    ("vision", "Vision", "image/screenshot analysis"),
    ("compression", "Compression", "context summarization"),
    ("web_extract", "Web extract", "web page summarization"),
    ("approval", "Approval", "smart command approval"),
    ("mcp", "MCP", "MCP tool reasoning"),
    ("title_generation", "Title generation", "session titles"),
    ("memory_query_rewrite", "Memory query rewrite", "memory retrieval queries"),
    ("skills_hub", "Skills hub", "skills search/install"),
    ("curator", "Curator", "skill-usage review pass"),
]

# Special non-auxiliary task surfaced in the same picker: subagent delegation.
# Routing lives under top-level `delegation.*` in config.yaml (NOT
# `auxiliary.delegation`) because delegate_task spawns full child agents via
# tools/delegate_tool.py::_resolve_delegation_credentials(), which reads the
# delegation section directly. "auto" here means "inherit the parent agent's
# provider/model/credentials" and is stored as empty strings — never persist
# the literal "auto", or it would be resolved as a provider name.
_DELEGATION_TASK_KEY = "delegation"
_DELEGATION_TASK_NAME = "Delegation"
_DELEGATION_TASK_DESC = "subagent model (delegate_task)"


def _all_aux_tasks() -> list[tuple[str, str, str]]:
    """Return built-in + plugin-registered auxiliary tasks for picker/menu use.

    Built-in tasks come first (preserving order), followed by plugin tasks
    sorted by key. Used by ``_aux_config_menu``, ``_reset_aux_to_auto``, and
    display-name lookups so plugin-registered tasks (registered via
    :meth:`son_of_anton_cli.plugins.PluginContext.register_auxiliary_task`) appear
    in the same surfaces as built-in ones without core knowing about them.
    """
    tasks = list(_AUX_TASKS)
    try:
        from son_of_anton_cli.plugins import get_plugin_auxiliary_tasks
        for entry in get_plugin_auxiliary_tasks():
            tasks.append((entry["key"], entry["display_name"], entry["description"]))
    except Exception:
        # Plugin discovery failure must not break the aux config UI.
        # Built-in tasks remain available.
        pass
    return tasks


def _format_aux_current(task_cfg: dict) -> str:
    """Render the current aux config for display in the task menu."""
    if not isinstance(task_cfg, dict):
        return "auto"
    base_url = str(task_cfg.get("base_url") or "").strip()
    provider = str(task_cfg.get("provider") or "auto").strip() or "auto"
    model = str(task_cfg.get("model") or "").strip()
    if base_url:
        short = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        return f"custom ({short})" + (f" · {model}" if model else "")
    if provider == "auto":
        return "auto" + (f" · {model}" if model else "")
    if model:
        return f"{provider} · {model}"
    return provider


def _delegation_cfg_as_task(cfg: dict) -> dict:
    """Project the top-level ``delegation`` section into aux-task shape.

    Returns a dict with provider/model/base_url/api_key keys so the shared
    rendering (``_format_aux_current``) and picker code can treat delegation
    like any other task. Empty provider means "inherit parent" which renders
    as "auto".
    """
    d = cfg.get("delegation")
    if not isinstance(d, dict):
        d = {}
    return {
        "provider": str(d.get("provider") or "").strip(),
        "model": str(d.get("model") or "").strip(),
        "base_url": str(d.get("base_url") or "").strip(),
        "api_key": str(d.get("api_key") or "").strip(),
    }


def _aux_task_display_name(task: str) -> str:
    """Display name for a task key, covering the special delegation entry."""
    if task == _DELEGATION_TASK_KEY:
        return _DELEGATION_TASK_NAME
    return next((name for key, name, _ in _all_aux_tasks() if key == task), task)


def _save_aux_choice(
    task: str,
    *,
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
) -> None:
    """Persist an auxiliary task's provider/model to config.yaml.

    Only writes the four routing fields — timeout, download_timeout, and any
    other task-specific settings are preserved untouched. The main model
    config (``model.default``/``model.provider``) is never modified.

    The special ``delegation`` task writes to the top-level ``delegation``
    section (consumed by ``tools/delegate_tool.py``), not ``auxiliary.*``.
    There, "auto" (inherit the parent agent) is stored as an empty provider —
    the literal string "auto" would be resolved as a provider name.
    """
    from son_of_anton_cli.config import load_config, save_config

    cfg = load_config()

    if task == _DELEGATION_TASK_KEY:
        entry = cfg.setdefault("delegation", {})
        if not isinstance(entry, dict):
            entry = {}
            cfg["delegation"] = entry
        entry["provider"] = "" if provider == "auto" else provider
        entry["model"] = model or ""
        entry["base_url"] = base_url or ""
        entry["api_key"] = api_key or ""
        save_config(cfg)
        return

    aux = cfg.setdefault("auxiliary", {})
    if not isinstance(aux, dict):
        aux = {}
        cfg["auxiliary"] = aux
    entry = aux.setdefault(task, {})
    if not isinstance(entry, dict):
        entry = {}
        aux[task] = entry
    entry["provider"] = provider
    entry["model"] = model or ""
    entry["base_url"] = base_url or ""
    entry["api_key"] = api_key or ""
    save_config(cfg)


def _reset_aux_to_auto() -> int:
    """Reset every known aux task back to auto/empty. Returns number reset.

    Includes plugin-registered tasks (via ``_all_aux_tasks``) so a plugin
    that contributed an auxiliary task gets reset alongside built-ins.
    """
    from son_of_anton_cli.config import load_config, save_config

    cfg = load_config()
    aux = cfg.setdefault("auxiliary", {})
    if not isinstance(aux, dict):
        aux = {}
        cfg["auxiliary"] = aux
    count = 0
    for task, _name, _desc in _all_aux_tasks():
        entry = aux.setdefault(task, {})
        if not isinstance(entry, dict):
            entry = {}
            aux[task] = entry
        changed = False
        if entry.get("provider") not in {None, "", "auto"}:
            entry["provider"] = "auto"
            changed = True
        for field in ("model", "base_url", "api_key"):
            if entry.get(field):
                entry[field] = ""
                changed = True
        # Preserve timeout/download_timeout — those are user-tuned, not routing
        if changed:
            count += 1
    # Delegation (top-level section) — clear only the routing fields; other
    # delegation settings (max_concurrent_children, max_spawn_depth, etc.)
    # are not routing and must be preserved.
    dele = cfg.get("delegation")
    if isinstance(dele, dict):
        changed = False
        for field in ("provider", "model", "base_url", "api_key"):
            if dele.get(field):
                dele[field] = ""
                changed = True
        if changed:
            count += 1
    save_config(cfg)
    return count


def _aux_config_menu() -> None:
    """Top-level auxiliary-model picker — choose a task to configure.

    Loops until the user picks "Back" so multiple tasks can be configured
    without returning to the main provider menu.
    """
    from son_of_anton_cli.config import load_config

    while True:
        cfg = load_config()
        aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}

        print()
        print("  Auxiliary models — side-task routing")
        print()
        print("  Side tasks (vision, compression, web extraction, etc.) default")
        print('  to your main chat model.  "auto" means "use my main model".')
        print("  Override a task below if you want it pinned to a specific")
        print("  provider/model.")
        print()

        # Build the task menu with current settings inline
        all_tasks = _all_aux_tasks()
        menu_tasks = all_tasks + [
            (_DELEGATION_TASK_KEY, _DELEGATION_TASK_NAME, _DELEGATION_TASK_DESC)
        ]
        name_col = max(len(name) for _, name, _ in menu_tasks) + 2
        desc_col = max(len(desc) for _, _, desc in menu_tasks) + 4
        entries: list[tuple[str, str]] = []
        for task_key, name, desc in menu_tasks:
            if task_key == _DELEGATION_TASK_KEY:
                task_cfg = _delegation_cfg_as_task(cfg)
            else:
                task_cfg = (
                    aux.get(task_key, {}) if isinstance(aux.get(task_key), dict) else {}
                )
            current = _format_aux_current(task_cfg)
            label = (
                f"{name.ljust(name_col)}{('(' + desc + ')').ljust(desc_col)}{current}"
            )
            entries.append((task_key, label))
        entries.append(("__reset__", "Reset all to auto"))
        entries.append(("__back__", "Back"))

        idx = _prompt_provider_choice(
            [label for _, label in entries],
            default=0,
        )
        if idx is None:
            return
        key = entries[idx][0]
        if key == "__back__":
            return
        if key == "__reset__":
            n = _reset_aux_to_auto()
            if n:
                print(f"Reset {n} auxiliary task(s) to auto.")
            else:
                print("All auxiliary tasks were already set to auto.")
            print()
            continue
        # Otherwise configure the specific task
        _aux_select_for_task(key)


def _aux_select_for_task(task: str) -> None:
    """Pick a provider + model for a single auxiliary task and persist it.

    Provider rows come from ``build_aux_picker_rows()`` — the shared aux-picker
    substrate — so this surface shows exactly what every other aux picker
    shows: authenticated built-ins, the user's own ``providers:`` /
    ``custom_providers:`` endpoints, and providers whose credential pool is
    temporarily exhausted. Only already-configured providers appear; users set
    up new ones through the normal ``son-of-anton model`` flow, then route aux tasks
    to them here.
    """
    from son_of_anton_cli.config import load_config
    from son_of_anton_cli.inventory import build_aux_picker_rows, format_aux_picker_entries

    cfg = load_config()
    if task == _DELEGATION_TASK_KEY:
        task_cfg = _delegation_cfg_as_task(cfg)
    else:
        aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
        task_cfg = aux.get(task, {}) if isinstance(aux.get(task), dict) else {}
    current_provider = str(task_cfg.get("provider") or "auto").strip() or "auto"
    current_model = str(task_cfg.get("model") or "").strip()
    current_base_url = str(task_cfg.get("base_url") or "").strip()

    display_name = _aux_task_display_name(task)

    # Gather authenticated providers (has credentials + curated model list)
    try:
        providers = build_aux_picker_rows(
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
        )
    except Exception as exc:
        print(f"Could not detect authenticated providers: {exc}")
        providers = []

    entries: list[tuple[str, str, list[str]]] = []  # (slug, label, models)
    # "auto" always first
    auto_marker = (
        "  ← current" if current_provider == "auto" and not current_base_url else ""
    )
    auto_label = (
        "auto (inherit main agent)"
        if task == _DELEGATION_TASK_KEY
        else "auto (recommended)"
    )
    entries.append(("__auto__", f"{auto_label}{auto_marker}", []))

    entries.extend(
        format_aux_picker_entries(
            providers,
            current_provider=current_provider,
            current_base_url=current_base_url,
        )
    )

    # Custom endpoint (raw base_url)
    custom_marker = "  ← current" if current_base_url else ""
    entries.append(("__custom__", f"Custom endpoint (direct URL){custom_marker}", []))
    entries.append(("__back__", "Back", []))

    print()
    print(f"  Configure {display_name} — current: {_format_aux_current(task_cfg)}")
    print()

    idx = _prompt_provider_choice([label for _, label, _ in entries], default=0)
    if idx is None:
        return
    slug, _label, models = entries[idx]

    if slug == "__back__":
        return

    if slug == "__auto__":
        _save_aux_choice(task, provider="auto", model="", base_url="", api_key="")
        print(f"{display_name}: reset to auto.")
        return

    if slug == "__custom__":
        _aux_flow_custom_endpoint(task, task_cfg)
        return

    # Regular provider — pick a model from its curated list
    _aux_flow_provider_model(task, slug, models, current_model)


def _aux_flow_provider_model(
    task: str,
    provider_slug: str,
    curated_models: list,
    current_model: str = "",
) -> None:
    """Prompt for a model under an already-authenticated provider, save to aux."""
    from son_of_anton_cli.auth import _prompt_model_selection
    from son_of_anton_cli.models import get_pricing_for_provider

    display_name = _aux_task_display_name(task)

    # Fetch live pricing for this provider (non-blocking)
    pricing: dict = {}
    try:
        pricing = get_pricing_for_provider(provider_slug) or {}
    except Exception:
        pricing = {}

    model_list = list(curated_models)

    # Let the user pick a model. _prompt_model_selection supports "Enter custom
    # model name" and cancel.  When there's no curated list (rare), fall back
    # to a raw input prompt.
    if not model_list:
        print(f"No curated model list for {provider_slug}.")
        print("Enter a model slug manually (blank = use provider default):")
        try:
            val = line_input("Model: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        selected = val or ""
    else:
        selected = _prompt_model_selection(
            model_list,
            current_model=current_model,
            pricing=pricing,
            confirm_provider=provider_slug,
        )
        if selected is None:
            print("No change.")
            return

    _save_aux_choice(
        task, provider=provider_slug, model=selected or "", base_url="", api_key=""
    )
    if selected:
        print(f"{display_name}: {provider_slug} · {selected}")
    else:
        print(f"{display_name}: {provider_slug} (provider default model)")


def _aux_flow_custom_endpoint(task: str, task_cfg: dict) -> None:
    """Prompt for a direct OpenAI-compatible base_url + optional api_key/model."""
    from son_of_anton_cli.secret_prompt import masked_secret_prompt

    display_name = _aux_task_display_name(task)
    current_base_url = str(task_cfg.get("base_url") or "").strip()
    current_model = str(task_cfg.get("model") or "").strip()

    print()
    print(f"  Custom endpoint for {display_name}")
    print("  Provide an OpenAI-compatible base URL (e.g. http://localhost:11434/v1)")
    print()
    try:
        url_prompt = (
            f"Base URL [{current_base_url}]: " if current_base_url else "Base URL: "
        )
        url = line_input(url_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return
    url = url or current_base_url
    if not url:
        print("No URL provided. No change.")
        return
    try:
        model_prompt = (
            f"Model slug (optional) [{current_model}]: "
            if current_model
            else "Model slug (optional): "
        )
        model = line_input(model_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return
    model = model or current_model
    try:
        api_key = masked_secret_prompt(
            "API key (optional, blank = use OPENAI_API_KEY): "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    _save_aux_choice(
        task,
        provider="custom",
        model=model,
        base_url=url,
        api_key=api_key,
    )
    short_url = url.replace("https://", "").replace("http://", "").rstrip("/")
    print(f"{display_name}: custom ({short_url})" + (f" · {model}" if model else ""))


def _prompt_provider_choice(choices, *, default=0, title="Select provider:"):
    """Show provider selection menu with curses arrow-key navigation.

    Falls back to a numbered list when curses is unavailable (e.g. piped
    stdin, non-TTY environments).  Returns the selected index, or None
    if the user cancels.
    """
    try:
        from son_of_anton_cli.setup import _curses_prompt_choice

        idx = _curses_prompt_choice(title, choices, default)
        if idx >= 0:
            print()
            return idx
    except Exception:
        pass

    # Fallback: numbered list
    print(title)
    for i, c in enumerate(choices, 1):
        marker = "→" if i - 1 == default else " "
        print(f"  {marker} {i}. {c}")
    print()
    while True:
        try:
            val = input(f"Choice [1-{len(choices)}] ({default + 1}): ").strip()
            if not val:
                return default
            idx = int(val) - 1
            if 0 <= idx < len(choices):
                return idx
            print(f"Please enter 1-{len(choices)}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            print()
            return None










_DEFAULT_QWEN_PORTAL_MODELS = [
    "qwen3-coder-plus",
    "qwen3-coder",
]


def _prompt_custom_api_mode_selection(base_url: str, current_api_mode: str = "") -> Optional[str]:
    """Prompt for a custom provider API mode.

    Returns an explicit mode string, or None to keep auto-detect behavior.
    """
    from son_of_anton_cli.runtime_provider import _detect_api_mode_for_url

    detected_mode = _detect_api_mode_for_url(base_url)
    normalized_current = str(current_api_mode or "").strip().lower()
    default_mode = normalized_current or detected_mode or ""

    mode_options = [
        (
            "",
            "Auto-detect",
            "Use Son of Anton URL heuristics; best for standard OpenAI-compatible endpoints.",
        ),
        (
            "chat_completions",
            "Chat Completions",
            "Use /chat/completions for standard OpenAI-compatible servers.",
        ),
        (
            "codex_responses",
            "Responses / Codex",
            "Use /responses for Codex-compatible tool-calling backends.",
        ),
        (
            "Anthropic Messages",
            "Use /v1/messages for Anthropic-compatible endpoints.",
        ),
    ]

    print()
    print("Select API compatibility mode:")
    for idx, (value, label, description) in enumerate(mode_options, 1):
        markers = []
        if value == detected_mode:
            markers.append("detected")
        if value == default_mode:
            markers.append("current")
        suffix = f" [{' / '.join(markers)}]" if markers else ""
        print(f"  {idx}. {label}{suffix}")
        print(f"     {description}")

    try:
        raw = input(
            "Choice [1-4, Enter to keep current/detected]: "
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        raise

    if not raw:
        return default_mode or None

    if raw in {"1", "auto", "detect", "auto-detect"}:
        return None
    if raw in {"2", "chat", "chat_completions", "completions"}:
        return "chat_completions"
    if raw in {"3", "responses", "codex", "codex_responses"}:
        return "codex_responses"

    print(f"Invalid API mode choice: {raw}. Falling back to auto-detect.")
    return None


def _auto_provider_name(base_url: str) -> str:
    """Generate a display name from a custom endpoint URL.

    Returns a human-friendly label like "Local (localhost:11434)" or
    "RunPod (xyz.runpod.io)".  Used as the default when prompting the
    user for a display name during custom endpoint setup.
    """
    import re

    clean = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    clean = re.sub(r"/v1/?$", "", clean)
    name = clean.split("/")[0]
    if "localhost" in name or "127.0.0.1" in name:
        name = f"Local ({name})"
    elif "runpod" in name.lower():
        name = f"RunPod ({name})"
    else:
        name = name.capitalize()
    return name


def _custom_provider_api_key_config_value(provider_info, resolved_api_key=""):
    """Return the value that should be persisted for a custom provider key."""
    api_key_ref = str(provider_info.get("api_key_ref", "") or "").strip()
    if api_key_ref:
        return api_key_ref

    key_env = str(provider_info.get("key_env", "") or "").strip()
    if key_env and not str(provider_info.get("api_key", "") or "").strip():
        return f"${{{key_env}}}"

    return str(resolved_api_key or "").strip()


def _custom_provider_base_url_config_value(provider_info, resolved_base_url=""):
    """Return the value that should be persisted for a custom provider URL."""
    base_url_ref = str(provider_info.get("base_url_ref", "") or "").strip()
    if base_url_ref:
        return base_url_ref
    return str(resolved_base_url or "").strip()


def _save_custom_provider(
    base_url, api_key="", model="", context_length=None, name=None, api_mode=None,
    key_env=""
):
    """Save a custom endpoint to custom_providers in config.yaml.

    Deduplicates by base_url — if the URL already exists, updates the
    model name, context_length, and api_mode but doesn't add a duplicate entry.
    Uses *name* when provided, otherwise auto-generates from the URL.

    When *key_env* is set the caller has already written the key to ``.env``,
    so the entry references it instead of inlining the secret (#69449).
    """
    from son_of_anton_cli.config import load_config, save_config

    cfg = load_config()
    providers = cfg.get("custom_providers") or []
    if not isinstance(providers, list):
        providers = []

    # Check if this URL is already saved — update model/context_length if so
    for entry in providers:
        if isinstance(entry, dict) and entry.get("base_url", "").rstrip(
            "/"
        ) == base_url.rstrip("/"):
            changed = False
            if model and entry.get("model") != model:
                entry["model"] = model
                changed = True
            if model and context_length:
                models_cfg = entry.get("models", {})
                if not isinstance(models_cfg, dict):
                    models_cfg = {}
                models_cfg[model] = {"context_length": context_length}
                entry["models"] = models_cfg
                changed = True
            if api_mode:
                if entry.get("api_mode") != api_mode:
                    entry["api_mode"] = api_mode
                    changed = True
            elif "api_mode" in entry:
                entry.pop("api_mode", None)
                changed = True
            if key_env and (entry.get("key_env") != key_env or entry.get("api_key")):
                entry["key_env"] = key_env
                entry.pop("api_key", None)
                changed = True
            if changed:
                cfg["custom_providers"] = providers
                save_config(cfg)
            return  # already saved, updated if needed

    # Use provided name or auto-generate from URL
    if not name:
        name = _auto_provider_name(base_url)

    entry = {"name": name, "base_url": base_url}
    if key_env:
        entry["key_env"] = key_env
    elif api_key:
        entry["api_key"] = api_key
    if model:
        entry["model"] = model
    if api_mode:
        entry["api_mode"] = api_mode
    if model and context_length:
        entry["models"] = {model: {"context_length": context_length}}

    providers.append(entry)
    cfg["custom_providers"] = providers
    save_config(cfg)
    print(f'  💾 Saved to custom providers as "{name}" (edit in config.yaml)')




def _remove_custom_provider(config):
    """Let the user remove a saved custom provider from config.yaml."""
    from son_of_anton_cli.config import load_config, save_config

    cfg = load_config()
    providers = cfg.get("custom_providers") or []
    if not isinstance(providers, list) or not providers:
        print("No custom providers configured.")
        return

    print("Remove a custom provider:\n")

    choices = []
    for entry in providers:
        if isinstance(entry, dict):
            name = entry.get("name", "unnamed")
            url = entry.get("base_url", "")
            short_url = url.replace("https://", "").replace("http://", "").rstrip("/")
            choices.append(f"{name} ({short_url})")
        else:
            choices.append(str(entry))
    choices.append("Cancel")

    try:
        from son_of_anton_cli.curses_ui import curses_radiolist

        idx = curses_radiolist(
            "Select provider to remove:",
            list(choices),
            selected=0,
            cancel_returns=-1,
        )
        print()
        if idx < 0:
            idx = None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        for i, c in enumerate(choices, 1):
            print(f"  {i}. {c}")
        print()
        try:
            val = input(f"Choice [1-{len(choices)}]: ").strip()
            idx = int(val) - 1 if val else None
        except (ValueError, KeyboardInterrupt, EOFError):
            idx = None

    if idx is None or idx >= len(providers):
        print("No change.")
        return

    removed = providers.pop(idx)
    cfg["custom_providers"] = providers
    save_config(cfg)
    removed_name = (
        removed.get("name", "unnamed") if isinstance(removed, dict) else str(removed)
    )
    print(f'✅ Removed "{removed_name}" from custom providers.')




# Lazy-export the model catalog at module level. Tests and a handful of
# downstream call sites read `son_of_anton_cli.main._PROVIDER_MODELS` directly,
# so the symbol needs to be reachable as a module attribute. But importing
# the catalog eagerly costs ~55ms on every `son-of-anton` invocation — including
# fast paths like `son-of-anton --version` and slash-command dispatch that never
# touch the catalog. PEP 562 module-level __getattr__ defers the import
# until first attribute access, so the cost is only paid by callers that
# actually look up the catalog.
_LAZY_MODEL_EXPORTS = ("_PROVIDER_MODELS",)


# The main.py decomposition moved the sessions/update command implementations
# into their own modules, but main.py still re-exports their surface so
# argparse wiring and test monkeypatches on son_of_anton_cli.main.<name> keep
# resolving unchanged. Importing those modules eagerly costs ~50ms on
# every `son-of-anton` invocation, including fast paths like `son-of-anton --version`
# that never run a subcommand. Resolve the re-exports through the module
# __getattr__ below instead, so each module is only imported when one of its
# names is actually touched. Monkeypatching keeps working: patch.object sets
# a real module attribute, which shadows __getattr__.
_LAZY_COMMAND_EXPORTS = {
    "son_of_anton_cli.sessions_cmd": (
        "cmd_sessions",
    ),
    "son_of_anton_cli.update_cmd": (
        "_add_upstream_remote",
        "_atomic_replace_dir",
        "_capture_active_lazy_features",
        "_capture_active_tool_dependencies",
        "_capture_head_sha",
        "_assess_parked_branch_switch",
        "_branch_head_label",
        "_branch_head_suffix",
        "_cmd_update_check",
        "_cmd_update_impl",
        "_count_commits_between",
        "_discard_lockfile_churn",
        "_discard_stashed_changes",
        "_park_stashed_changes",
        "_ensure_fhs_path_guard",
        "_for_each_systemd_gateway_unit",
        "_format_time_ago",
        "_purge_stale_son_of_anton_modules",
        "_gateway_prompt",
        "_get_origin_url",
        "_has_upstream_remote",
        "_invalidate_update_cache",
        "_is_fork",
        "_log_only_write",
        "_mark_skip_upstream_prompt",
        "_npm_bin_exists",
        "_npm_lockfile_changed",
        "_npm_manifest_paths",
        "_npm_manifests_digest",
        "_print_curator_first_run_notice",
        "_print_curator_recent_run_notice",
        "_print_fts_optimize_available_notice",
        "_print_parked_branch_skip_warning",
        "_print_stash_cleanup_guidance",
        "_print_update_completion",
        "_record_npm_lockfile_hash",
        "_refresh_active_lazy_features",
        "_refresh_active_memory_provider_dependencies",
        "_refresh_bootstrap_cache_scripts",
        "_reload_updated_runtime_modules",
        "_resolve_pre_update_backup_mode",
        "_resolve_stash_selector",
        "_restart_phase_failure_is_incomplete",
        "_restore_active_tool_dependencies",
        "_restore_stashed_changes",
        "_run_logged_subprocess",
        "_run_pre_update_backup",
        "_service_unit_supports_graceful_sigusr1_restart",
        "_should_skip_upstream_prompt",
        "_stash_apply_failed_only_on_existing_untracked",
        "_stash_local_changes_if_needed",
        "_surviving_gateway_pids_after_failed_restart",
        "_sync_fork_with_upstream",
        "_sync_with_upstream_if_needed",
        "_update_node_dependencies",
        "_upgrade_pip_before_lazy_refresh",
        "_validate_critical_files_syntax",
        "_validate_critical_modules_import",
        "_venv_core_imports_healthy",
        "_warn_gateway_restart_phase_aborted",
        "_warn_incomplete_gateway_fleet_restart",
        "_write_lazy_refresh_incomplete_marker",
        "_write_marker_file",
        "_write_update_incomplete_marker",
        "_UPDATE_RUNTIME_RELOAD_MODULES",
        "_UPDATE_CRITICAL_FILES",
        "_UPDATE_CRITICAL_MODULES",
        "OFFICIAL_REPO_URLS",
        "OFFICIAL_REPO_URL",
        "SKIP_UPSTREAM_PROMPT_FILE",
        "_PRE_UPDATE_SNAPSHOT_KEEP",
        "_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE",
    ),
}

_LAZY_COMMAND_ATTR_TO_MODULE = {
    attr: module for module, attrs in _LAZY_COMMAND_EXPORTS.items() for attr in attrs
}



def _self():
    """This module, for attribute access at call time.

    Bare-name global lookups inside this module do not go through the PEP 562
    __getattr__ below, so internal callers of the lazily re-exported names use
    _self().<name> instead. That resolves the lazy re-export on first use and
    keeps monkeypatches on son_of_anton_cli.main.<name> working, exactly like a
    globals lookup did. ``sys`` is imported locally because some tests patch
    this module's ``sys`` attribute.
    """
    import sys as _sys

    return _sys.modules[__name__]


def __getattr__(name):
    """Defer the model-catalog and command-module imports until first read."""
    if name in _LAZY_MODEL_EXPORTS:
        from son_of_anton_cli.models import _PROVIDER_MODELS
        # Cache on the module so subsequent accesses skip the import machinery.
        globals()[name] = _PROVIDER_MODELS
        return _PROVIDER_MODELS
    module = _LAZY_COMMAND_ATTR_TO_MODULE.get(name)
    if module is not None:
        import importlib

        value = getattr(importlib.import_module(module), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _current_reasoning_effort(config) -> str:
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("reasoning_effort") or "").strip().lower()
    return ""


def _set_reasoning_effort(config, effort: str) -> None:
    agent_cfg = config.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config["agent"] = agent_cfg
    agent_cfg["reasoning_effort"] = effort


def _prompt_reasoning_effort_selection(efforts, current_effort=""):
    """Prompt for a reasoning effort. Returns effort, 'none', or None to keep current."""
    deduped = list(
        dict.fromkeys(
            str(effort).strip().lower() for effort in efforts if str(effort).strip()
        )
    )
    canonical_order = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    ordered = [effort for effort in canonical_order if effort in deduped]
    ordered.extend(effort for effort in deduped if effort not in canonical_order)
    if not ordered:
        return None

    def _label(effort):
        if effort == current_effort:
            return f"{effort}  ← currently in use"
        return effort

    disable_label = "Disable reasoning"
    skip_label = "Skip (keep current)"

    if current_effort == "none":
        default_idx = len(ordered)
    elif current_effort in ordered:
        default_idx = ordered.index(current_effort)
    elif "medium" in ordered:
        default_idx = ordered.index("medium")
    else:
        default_idx = 0

    try:
        from son_of_anton_cli.curses_ui import curses_radiolist

        choices = [_label(effort) for effort in ordered]
        choices.append(disable_label)
        choices.append(skip_label)
        idx = curses_radiolist(
            "Select reasoning effort:",
            choices,
            selected=default_idx,
            cancel_returns=-1,
        )
        if idx < 0:
            return None
        print()
        if idx < len(ordered):
            return ordered[idx]
        if idx == len(ordered):
            return "none"
        return None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    print("Select reasoning effort:")
    for i, effort in enumerate(ordered, 1):
        print(f"  {i}. {_label(effort)}")
    n = len(ordered)
    print(f"  {n + 1}. {disable_label}")
    print(f"  {n + 2}. {skip_label}")
    print()

    while True:
        try:
            choice = input(f"Choice [1-{n + 2}] (default: keep current): ").strip()
            if not choice:
                return None
            idx = int(choice)
            if 1 <= idx <= n:
                return ordered[idx - 1]
            if idx == n + 1:
                return "none"
            if idx == n + 2:
                return None
            print(f"Please enter 1-{n + 2}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            return None






def _prompt_api_key(
    pconfig,
    existing_key: str,
    provider_id: str = "",
    existing_source: str = "",
) -> tuple:
    """Shared API-key entry point for ``son-of-anton setup`` / ``son-of-anton model``.

    Handles both first-time entry and the already-configured case.  When a key
    is already present, offers [K]eep / [R]eplace / [C]lear so the user can
    recover from a malformed paste without editing ``~/.son-of-anton/.env`` by hand.

    Returns ``(resolved_key, abort)``.  ``abort=True`` means the caller should
    ``return`` immediately — the user cancelled entry, declined to replace, or
    cleared the key and is now unconfigured.
    """
    from son_of_anton_cli.auth import LMSTUDIO_NOAUTH_PLACEHOLDER
    from son_of_anton_cli.config import save_env_value
    from son_of_anton_cli.secret_prompt import masked_secret_prompt

    key_env = pconfig.api_key_env_vars[0] if pconfig.api_key_env_vars else ""

    def _prompt_new_key(*, allow_lmstudio_default: bool) -> str:
        if provider_id == "lmstudio" and allow_lmstudio_default:
            prompt = f"{key_env} (Enter for no-auth default {LMSTUDIO_NOAUTH_PLACEHOLDER!r}): "
        else:
            prompt = f"{key_env} (or Enter to cancel): "
        try:
            entered = masked_secret_prompt(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return ""
        if not entered and provider_id == "lmstudio" and allow_lmstudio_default:
            return LMSTUDIO_NOAUTH_PLACEHOLDER
        return entered

    # First-time entry ────────────────────────────────────────────────────
    if not existing_key:
        print(f"No {pconfig.name} API key configured.")
        if not key_env:
            return "", True
        new_key = _prompt_new_key(allow_lmstudio_default=True)
        if not new_key:
            print("Cancelled.")
            return "", True
        save_env_value(key_env, new_key)
        print("API key saved.")
        print()
        return new_key, False

    # Already configured — offer K / R / C ────────────────────────────────
    from son_of_anton_cli.env_loader import format_secret_source_suffix

    source_suffix = format_secret_source_suffix(key_env) if key_env else ""
    print(f"  {pconfig.name} API key: {existing_key[:8]}... ✓{source_suffix}")
    if not key_env:
        # Nothing we can rewrite; just acknowledge and move on.
        print()
        return existing_key, False
    pool_backed = existing_source.startswith("credential_pool:")
    menu = (
        "  [K]eep / [R]eplace (default K): "
        if pool_backed
        else "  [K]eep / [R]eplace / [C]lear (default K): "
    )
    try:
        choice = input(menu).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        choice = "k"

    if choice.startswith("r"):
        new_key = _prompt_new_key(allow_lmstudio_default=False)
        if not new_key:
            print("  No change.")
            print()
            return existing_key, False
        save_env_value(key_env, new_key)
        print("  API key updated.")
        print()
        return new_key, False

    if choice.startswith("c") and not pool_backed:
        save_env_value(key_env, "")
        print(
            f"  API key cleared.  Re-run `son-of-anton setup` to configure {pconfig.name} again."
        )
        return "", True

    # Keep (default, or any other input)
    print()
    return existing_key, False




def _infer_stepfun_region(base_url: str) -> str:
    """Infer the current StepFun region from the configured endpoint."""
    normalized = (base_url or "").strip().lower()
    if "api.stepfun.com" in normalized:
        return "china"
    return "international"


def _stepfun_base_url_for_region(region: str) -> str:
    from son_of_anton_cli.auth import (
        STEPFUN_STEP_PLAN_CN_BASE_URL,
        STEPFUN_STEP_PLAN_INTL_BASE_URL,
    )

    return (
        STEPFUN_STEP_PLAN_CN_BASE_URL
        if region == "china"
        else STEPFUN_STEP_PLAN_INTL_BASE_URL
    )










def cmd_status(args):
    """Show status of all components."""
    from son_of_anton_cli.status import show_status

    show_status(args)


def cmd_cron(args):
    """Cron job management."""
    from son_of_anton_cli.cron import cron_command

    cron_command(args)


def cmd_config(args):
    """Configuration management."""
    from son_of_anton_cli.config import config_command

    config_command(args)


def _print_version_info(*, check_updates: bool = True) -> None:
    # Single source of truth for version output — shared with the
    # `son-of-anton --version` pre-import fast path (the `version` subcommand
    # was consolidated into `--version`).
    _startup_fast.print_fast_version_info(check_updates=check_updates)


def cmd_version(args):
    """Show version (--version/-V flag)."""
    _print_version_info(check_updates=True)


def _clear_bytecode_cache(root: Path) -> int:
    """Remove all __pycache__ directories under *root*.

    Stale .pyc files can cause ImportError after code updates when Python
    loads a cached bytecode file that references names that no longer exist
    (or don't yet exist) in the updated source.  Clearing them forces Python
    to recompile from the .py source on next import.

    Returns the number of directories removed.
    """
    removed = 0
    for dirpath, dirnames, _ in os.walk(root):
        # Skip venv / node_modules / .git entirely
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {"venv", ".venv", "node_modules", ".git", ".worktrees"}
        ]
        if os.path.basename(dirpath) == "__pycache__":
            try:
                shutil.rmtree(dirpath)
                removed += 1
            except OSError:
                pass
            dirnames.clear()  # nothing left to recurse into
    return removed


# Update pipeline lives in son_of_anton_cli/update_cmd.py (main.py decomposition,
# mechanical move). Its names are re-exported lazily through the module-level
# __getattr__ above (see _LAZY_COMMAND_EXPORTS) so argparse wiring and test
# monkeypatches on son_of_anton_cli.main.<name> keep resolving unchanged without
# paying the update_cmd import cost on every CLI invocation.

# Stamp file recording the checkout fingerprint the bytecode cache was last
# validated against. Lives next to the checkout (NOT in SON_OF_ANTON_HOME) because
# __pycache__ is per-checkout state shared by every profile.
_BYTECODE_FINGERPRINT_FILE = ".bytecode-fingerprint"


def _record_bytecode_fingerprint() -> None:
    """Persist the current checkout fingerprint after a bytecode sweep.

    Never raises. A failed write just means the next launch re-sweeps —
    safe, merely redundant.
    """
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        tmp_path = stamp_path.with_name(stamp_path.name + ".tmp")
        tmp_path.write_text(fingerprint, encoding="utf-8")
        tmp_path.replace(stamp_path)
    except OSError as exc:
        logger.debug("Could not record bytecode fingerprint: %s", exc)


def _sweep_stale_bytecode_if_checkout_changed() -> None:
    """Clear ``__pycache__`` at launch when the checkout changed underneath us.

    The stale-bytecode bug class (issues #6207, #60242
    ``cannot import name 'parse_model_flags_detailed'`` report) has one
    shared shape: the checkout's ``.py`` files change (git pull inside
    ``son-of-anton update``, a manual ``git pull``, a ZIP update, a file-sync
    restore) while ``__pycache__`` retains bytecode from the previous
    revision, and a later process trusts the stale ``.pyc`` instead of the
    fresh source.

    Update-time clears alone can never close this class: ``son-of-anton update``
    always executes the PRE-pull updater code, so any hardening added to it
    only takes effect one update late, and manual ``git pull`` never runs
    the updater at all. This launch-time guard closes the loop: every
    ``son-of-anton`` entry point compares the checkout fingerprint (cheap file
    reads, no git subprocess) against the last-validated stamp and sweeps
    the bytecode cache once when they diverge.

    Never raises — a failure here must not block launch.
    """
    try:
        fingerprint = _read_git_revision_fingerprint(PROJECT_ROOT)
        if not fingerprint:
            return  # non-git install — the ZIP update path clears explicitly
        stamp_path = PROJECT_ROOT / _BYTECODE_FINGERPRINT_FILE
        try:
            recorded = stamp_path.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded == fingerprint:
            return
        removed = _clear_bytecode_cache(PROJECT_ROOT)
        if removed:
            logger.info(
                "Checkout changed since last launch (%s -> %s): cleared %d stale __pycache__ director%s",
                recorded or "unknown",
                fingerprint,
                removed,
                "y" if removed == 1 else "ies",
            )
        _record_bytecode_fingerprint()
    except Exception as exc:
        logger.debug("Stale-bytecode launch sweep failed: %s", exc)


def _nixos_build_env() -> dict[str, str] | None:
    """Return extra env vars for native module builds on NixOS.

    On NixOS, python3 is typically not on the system PATH (it lives in
    the Nix store and only enters PATH inside a nix-shell or when
    explicitly installed as a system package).  node-gyp uses Python to
    compile native addons like ``node-pty`` and its ``find-python.js``
    does a bare ``PATH`` lookup — which fails on NixOS.

    Two-tier resolution:
    1. Fast path — the son-of-anton venv's python3 (present in managed installs)
    2. Fallback — resolves the absolute python3 path via ``nix-shell``

    Returns an env dict suitable for ``subprocess.run(env=...)`` or
    ``None`` when we are not on NixOS or python3 is already on PATH.
    """
    import re

    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r"^ID=nixos$", os_release, re.M):
        return None

    # python3 already on PATH — nothing to do
    if shutil.which("python3"):
        return None

    # Resolve the absolute python3 path via nix-shell. Slower (~2–5 s for the
    # nix-shell eval) but always works. The resolved path is a self-contained
    # Nix store binary (all deps via RPATH) so it stays valid even after the
    # nix-shell exits.
    try:
        result = subprocess.run(
            ["nix-shell", "-p", "python3", "--run", "which python3"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15,
        )
        if result.returncode == 0:
            python3_path = result.stdout.strip()
            if python3_path and Path(python3_path).exists():
                return {**os.environ, "PYTHON": python3_path}
    except Exception:
        pass  # nix-shell not available — caller will get None

    return None
def _run_npm_install_deterministic(
    npm: str,
    cwd: Path,
    *,
    extra_args: tuple[str, ...] = (),
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a deterministic npm install that does not mutate ``package-lock.json``.

    Prefers ``npm ci`` (strict, lockfile-preserving) when a lockfile is present;
    falls back to ``npm install`` only if ``npm ci`` fails (e.g. lockfile out of
    sync on a WIP checkout).  Without this, ``npm install`` on npm ≥ 10 silently
    rewrites committed lockfiles (stripping ``"peer": true`` etc.), which leaves
    the working tree dirty and causes the next ``son-of-anton update`` to stash the
    lockfile — repeatedly.

    ``--include=dev`` is forced on every invocation: the callers are frontend
    builds (web UI / TUI / desktop workspaces), and those builds need the dev
    toolchain (``tsc``, ``vite``, ``electron-builder`` — all
    ``devDependencies``).  If the caller's environment has
    ``NODE_ENV=production`` (or npm config ``omit=dev``) — which leaks in from
    a shell profile, a container image, or the bundled TUI launcher that sets
    ``NODE_ENV=production`` on its subprocess env — npm silently omits
    devDependencies (exit 0, no error), so the build toolchain never installs
    and the subsequent build dies with ``tsc: command not found`` (exit 127).
    The flag overrides both the env var and npm config, unlike scrubbing
    ``NODE_ENV`` from the environment which only fixes the env-leak case.

    ``--no-save`` on the ``npm install`` fallback keeps it true to this
    function's contract: never mutate ``package-lock.json``.  Without it, an
    out-of-sync lockfile gets rewritten by the fallback, which drifts the
    committed lockfile and makes every future ``npm ci`` fail — a
    self-reinforcing cycle where web devDeps never install and a stale dist
    is served on every update (PR #65595).
    """
    # unicode-animations' postinstall animates to /dev/tty (bypasses
    # --silent/capture_output). It no-ops when CI is set — same as the TUI
    # install path and nix/lib.nix npm ci hooks.
    run_env = _npm_lifecycle_env(env)

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return _run_npm_watching_for_engine_failure(
            cmd,
            cwd=cwd,
            env=run_env,
            capture_output=capture_output,
        )

    def _attempt(npm_exe: str) -> subprocess.CompletedProcess:
        lockfile = cwd / "package-lock.json"
        if lockfile.exists():
            ci_result = _run([npm_exe, "ci", "--include=dev", *extra_args])
            if ci_result.returncode == 0:
                return ci_result
            # Fall through to `npm install` — lockfile may be out of sync on a
            # WIP fork/branch, or `npm ci` may not be available on very old npm.
        return _run([npm_exe, "install", "--no-save", "--include=dev", *extra_args])

    result = _attempt(npm)
    if result.returncode == 0:
        return result

    # An npm outside the root package.json's `engines.npm` range fails every
    # command here identically (the `npm install` fallback included), so the
    # failure is worth exactly one repair attempt. `maybe_repair_npm_engine`
    # returns the npm to retry with — the same one after an in-place upgrade
    # of a Son of Anton-managed install, or a freshly provisioned managed npm when
    # the failing npm belongs to the user's own toolchain.
    from son_of_anton_cli.npm_engine import maybe_repair_npm_engine

    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    repaired_npm = maybe_repair_npm_engine(npm, combined)
    if not repaired_npm:
        return result
    # The repaired npm may be a freshly provisioned managed one whose shebang
    # and lifecycle scripts resolve `node` from PATH — put the managed tree
    # first so they find the managed Node, not the mismatched system one.
    from son_of_anton_constants import with_son_of_anton_node_path

    run_env["PATH"] = with_son_of_anton_node_path(run_env)["PATH"]
    return _attempt(repaired_npm)


def _run_npm_watching_for_engine_failure(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture_output: bool,
) -> subprocess.CompletedProcess:
    """Run *cmd*, always retaining stderr so ``EBADENGINE`` stays detectable.

    ``capture_output=False`` callers stream npm's progress live and would
    otherwise hand back a ``CompletedProcess`` with ``stderr=None``, leaving the
    engine-failure recovery nothing to read. Tee stderr instead: each line is
    forwarded to this process's stderr as it arrives (so live output is
    unchanged) and accumulated for the caller.
    """
    if capture_output:
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    captured: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        if proc.stderr is not None:
            for line in proc.stderr:
                captured.append(line)
                sys.stderr.write(line)
            sys.stderr.flush()
        returncode = proc.wait()
    return subprocess.CompletedProcess(cmd, returncode, None, "".join(captured))



def _load_installable_optional_extras(group: str = "all") -> list[str]:
    """Return optional extras referenced by a dependency group.

    ``group`` is usually ``all`` (broad install).
    """
    try:
        import tomllib

        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except Exception:
        return []

    optional_deps = project.get("optional-dependencies", {})
    if not isinstance(optional_deps, dict):
        return []

    refs = optional_deps.get(group, [])
    referenced: list[str] = []
    for ref in refs:
        if "[" in ref and "]" in ref:
            name = ref.split("[", 1)[1].split("]", 1)[0]
            if name in optional_deps:
                referenced.append(name)

    return referenced


# Install-scoped breadcrumbs live next to the venv (not under $SON_OF_ANTON_HOME)
# because the venv is shared across profiles.
#
# ``.update-incomplete`` — generic core ``.[all]`` install was interrupted.
# Cleared only after a confirmed full dependency reinstall/recovery.
#
# ``.lazy-refresh-incomplete`` — lazy-backend refresh phase may have corrupted
# packages. Cleared only after import-probe repair confirms healthy (not when
# probes are unavailable/indeterminate). Narrow lazy probes must NEVER clear
# the generic core marker (#58004 review).
def _update_marker_path() -> Path:
    return PROJECT_ROOT / ".update-incomplete"


def _lazy_refresh_marker_path() -> Path:
    return PROJECT_ROOT / ".lazy-refresh-incomplete"


def _pytest_owns_live_checkout(root: Path) -> bool:
    """True when running under pytest AND ``root`` is this checkout itself.

    Tests that drive update/recovery without sandboxing ``PROJECT_ROOT``
    must neither litter the live repo root with recovery breadcrumbs
    (a leftover ``.lazy-refresh-incomplete`` / ``.update-incomplete``
    false-arms recovery on the developer's next real launch) nor run a real
    reinstall against the executing venv. Sandboxed tests point at a
    tmp_path and are unaffected (same posture as
    ``managed_scope._under_pytest``)."""
    return (
        "PYTEST_CURRENT_TEST" in os.environ
        and root == Path(__file__).resolve().parent.parent
    )


def _clear_marker_file(path: Path, *, label: str) -> None:
    """Remove an update-recovery breadcrumb. Never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not clear %s marker: %s", label, exc)


def _clear_update_incomplete_marker() -> None:
    """Remove the interrupted core-install breadcrumb. Never raises."""
    _clear_marker_file(_update_marker_path(), label="update-incomplete")


def _clear_lazy_refresh_incomplete_marker() -> None:
    """Remove the interrupted lazy-refresh breadcrumb. Never raises."""
    _clear_marker_file(_lazy_refresh_marker_path(), label="lazy-refresh-incomplete")


def _recover_from_interrupted_install() -> None:
    """Finish update work left half-done by a prior ``son-of-anton update``.

    Handles two independent breadcrumbs:

    - ``.update-incomplete`` — core ``.[all]`` install interrupted. Recovers
      via full quarantined reinstall. Never cleared by the narrow lazy-refresh
      import probes alone.
    - ``.lazy-refresh-incomplete`` — lazy-backend refresh may have corrupted
      packages. Recovers via package-only import probes; cleared only when
      probes confirm healthy/repaired (indeterminate keeps the marker).

    Never raises: a recovery failure must not block launch.  If it can't
    self-heal it prints the manual command and leaves the relevant marker so
    the next launch tries again.

    Concurrency: markers live next to the shared venv, so a gateway start
    plus a CLI launch (or two profiles starting at once) can both see them.
    An ``O_EXCL`` lockfile ensures only one process runs recovery; the
    others skip and let the winner clear markers.

    Output: everything — our status lines AND the streamed pip/uv install
    (which inherits fd 1) — is routed to stderr.  Launches whose stdout is a
    protocol stream (``son-of-anton acp`` speaks JSON-RPC on stdout) must never get
    install noise on stdout.
    """
    if _pytest_owns_live_checkout(PROJECT_ROOT):
        return
    core_marker = _update_marker_path().exists()
    lazy_marker = _lazy_refresh_marker_path().exists()
    if not core_marker and not lazy_marker:
        return

    # Skip in managed installs and on PyPI installs with no git checkout:
    # those don't run the source-tree update path, so a stray marker is not ours
    # to act on. Just clear it.
    if not (PROJECT_ROOT / "pyproject.toml").is_file():
        _clear_update_incomplete_marker()
        _clear_lazy_refresh_incomplete_marker()
        return

    # Single-flight guard: atomically claim the recovery lock. If another
    # process holds it, skip — it is running the same reinstall into the same
    # shared venv right now. A crashed holder leaves a stale lock; break it
    # after an hour (well past any realistic install) so recovery can't be
    # wedged forever.
    lock_path = PROJECT_ROOT / ".update-incomplete.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
    except FileExistsError:
        try:
            if _time.time() - lock_path.stat().st_mtime > 3600:
                lock_path.unlink()
        except OSError:
            pass
        return
    except OSError as exc:
        # Couldn't create the lock (read-only fs, perms). Proceed unlocked —
        # the install itself will surface the real problem.
        logger.debug("Could not create install-recovery lock: %s", exc)

    saved_stdout_fd = None
    saved_sys_stdout = sys.stdout
    try:
        # Route Python-level prints AND subprocess-inherited fd 1 to stderr
        # for the duration of recovery (see docstring: ACP stdout safety).
        try:
            saved_stdout_fd = os.dup(1)
            os.dup2(2, 1)
        except OSError:
            saved_stdout_fd = None
        sys.stdout = sys.stderr

        if lazy_marker:
            _recover_lazy_refresh_marker_locked()

        if _update_marker_path().exists():
            _recover_core_update_marker_locked()
    finally:
        sys.stdout = saved_sys_stdout
        if saved_stdout_fd is not None:
            try:
                os.dup2(saved_stdout_fd, 1)
                os.close(saved_stdout_fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def _recover_lazy_refresh_marker_locked() -> None:
    """Heal ``.lazy-refresh-incomplete`` via confirmed import-probe repair."""
    print(
        "⚠ A previous lazy-backend refresh may have left the venv unhealthy — "
        "running import-based package repair..."
    )
    install_prefix, install_env = _default_venv_install_target()
    status = _repair_venv_via_import_probes(install_prefix, env=install_env)
    if status in ("healthy", "repaired"):
        _clear_lazy_refresh_incomplete_marker()
        print("✓ Lazy-refresh venv recovery confirmed — install is healthy again.")
        return
    if status == "indeterminate":
        print(
            "  ⚠ Import probes unavailable — cannot confirm venv health. "
            "Leaving `.lazy-refresh-incomplete` for the next launch."
        )
    else:
        print(
            "  ⚠ Lazy-refresh package repair incomplete. "
            "Leaving `.lazy-refresh-incomplete` for the next launch."
        )
        print("  Recover manually with:")
        all_specs = _lazy_refresh_repair_specs(
            sorted(set(_LAZY_REFRESH_REPAIR_PACKAGES.values()))
        )
        print(
            f"    {' '.join(install_prefix)} install --force-reinstall "
            + " ".join(shlex.quote(s) for s in all_specs)
        )


def _recover_core_update_marker_locked() -> None:
    """Heal ``.update-incomplete`` via full ``.[all]`` reinstall only.

    Narrow lazy-refresh import probes are not sufficient proof that a generic
    interrupted core install finished — a missing dep outside that probe set
    would otherwise look healthy and clear the breadcrumb too early.
    """
    print(
        "⚠ A previous `son-of-anton update` was interrupted mid-install — "
        "finishing dependency installation now..."
    )

    try:
        from son_of_anton_cli import _install_repair as _ir

        # ensure_uv bootstraps the installer itself when missing (the early
        # pass's stdlib-only lookup cannot); keeping it here means the late
        # path still self-heals a venv whose uv vanished mid-update.
        from son_of_anton_cli.managed_uv import ensure_uv

        ensure_uv()

        # Delegate the install itself to the shared stdlib executor so both
        # this late path and the pre-import early pass run exactly the same
        # reinstall.  Called inside the same stdout→stderr redirect already
        # established by _recover_from_interrupted_install, so
        # run_core_install's own redirect nests harmlessly.
        _ir.run_core_install(PROJECT_ROOT)

        _clear_update_incomplete_marker()
        print("✓ Dependency installation recovered — your install is healthy again.")
    except Exception as exc:
        # Leave the marker in place so the next launch retries. Give the user
        # the exact manual recovery command in the meantime.
        logger.debug("Interrupted-install recovery failed: %s", exc)
        print("✗ Could not auto-recover the interrupted install.")
        print("  Recover manually with:")
        print(f"    cd {PROJECT_ROOT}")
        print(f"    {sys.executable} -m ensurepip --upgrade")
        print(f"    {sys.executable} -m pip install -e '.[all]'")


# Set on the re-exec'd child so it can never spawn another one.
_UPDATE_REEXEC_ENV = "SON_OF_ANTON_UPDATE_REEXEC"


def _default_venv_install_target() -> tuple[list[str], dict[str, str] | None]:
    """Return ``(install_cmd_prefix, env)`` for the project venv when possible."""
    try:
        from son_of_anton_cli.managed_uv import ensure_uv

        uv_bin = ensure_uv()
    except Exception:
        uv_bin = None
    if uv_bin:
        from son_of_anton_constants import project_venv_dir

        venv_dir = project_venv_dir(PROJECT_ROOT) or PROJECT_ROOT / "venv"
        env = {**os.environ, "VIRTUAL_ENV": str(venv_dir)}
        return [uv_bin, "pip"], env
    return [sys.executable, "-m", "pip"], None


def _run_install_with_heartbeat(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    heartbeat_interval_seconds: int = 30,
) -> None:
    """Run dependency install command with periodic heartbeat output.

    Some resolvers/build backends (especially when compiling Rust/C extensions)
    can stay quiet for minutes. Emit a simple elapsed-time heartbeat so users
    know ``son-of-anton update`` is still progressing even if pip/uv itself is silent.
    """
    done = threading.Event()
    start = _time.time()

    def _heartbeat() -> None:
        # Wait first, then print, so short installs don't emit noise.
        while not done.wait(heartbeat_interval_seconds):
            elapsed = int(_time.time() - start)
            print(
                f"  … still installing dependencies ({elapsed}s elapsed)"
                " — compiling Rust/C extensions can take several minutes",
                flush=True,
            )

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            env=env,
        )
    finally:
        done.set()
        t.join(timeout=0.2)


# Import probes for venv corruption after a failed lazy ``uv pip install``.
# Metadata can look fine while ``.py`` files were removed mid-install (#57828).
# Canonical tables live in the stdlib-only ``_early_recovery`` module (which
# also probes/repairs BEFORE this module's third-party imports can run) so the
# early and full recovery layers can never drift apart.
_LAZY_REFRESH_IMPORT_PROBES: tuple[tuple[str, str], ...] = (
    _early_recovery_mod.LAZY_REFRESH_IMPORT_PROBES
)

_LAZY_REFRESH_REPAIR_PACKAGES: dict[str, str] = (
    _early_recovery_mod.LAZY_REFRESH_REPAIR_PACKAGES
)


def _run_package_only_install(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Run a package-only pip/uv install without quarantining entry-point shims.

    ``pip install --upgrade pip`` and ``--force-reinstall <pkg>`` do not
    rewrite entry-point shims, so the editable-install quarantine path is
    not needed.
    """
    _run_install_with_heartbeat(cmd, env=env)


def _lazy_refresh_repair_specs(packages: list[str]) -> list[str]:
    """Map repair package names to their declared pin specs in pyproject.toml."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        return packages

    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return packages

    try:
        with open(pyproject, "rb") as f:
            raw_deps = tomllib.load(f).get("project", {}).get("dependencies", []) or []
    except Exception as exc:
        logger.debug("lazy refresh repair spec lookup failed: %s", exc)
        return packages

    name_to_spec: dict[str, str] = {}
    try:
        from packaging.requirements import Requirement  # type: ignore

        for spec in raw_deps:
            try:
                req = Requirement(spec)
                name_to_spec[req.name.lower()] = spec.split(";", 1)[0].strip()
            except Exception:
                continue
    except Exception:
        for spec in raw_deps:
            head = spec.split(";", 1)[0].strip()
            bare = head
            for op in ("==", ">=", "<=", "~=", ">", "<", "!="):
                if op in bare:
                    bare = bare.split(op, 1)[0]
                    break
            key = bare.strip().split("[", 1)[0].strip().lower()
            if key:
                name_to_spec[key] = head

    return [name_to_spec.get(pkg.lower(), pkg) for pkg in packages]


def _detect_broken_lazy_refresh_imports(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str] | None:
    """Probe lazy-refresh packages via real imports.

    Returns:
      - ``[]`` when probes ran and every package imported cleanly
      - ``[dist, ...]`` when probes ran and some packages failed
      - ``None`` when the probe could not run (missing venv Python, subprocess
        failure, non-zero probe exit) — this is *indeterminate*, not healthy
    """
    venv_python = _resolve_install_target_python(install_cmd_prefix, env)
    if venv_python is None:
        return None

    probe_lines = "\n".join(
        f"    ({mod!r}, {attr!r})," for mod, attr in _LAZY_REFRESH_IMPORT_PROBES
    )
    check_script = (
        "import os\n"
        "import sys\n"
        "probes = [\n"
        f"{probe_lines}\n"
        "]\n"
        "broken = []\n"
        "for mod, attr in probes:\n"
        "    try:\n"
        "        imported = __import__(mod)\n"
        "        if not hasattr(imported, attr):\n"
        "            broken.append(mod)\n"
        "        elif mod == 'certifi':\n"
        "            # The module can import cleanly while cacert.pem is\n"
        "            # missing/corrupt (brew Python upgrade, interrupted venv\n"
        "            # rebuild) - every TLS call then fails (#29866).\n"
        "            bundle = imported.where()\n"
        "            if not os.path.isfile(bundle) or os.path.getsize(bundle) < 1024:\n"
        "                broken.append(mod)\n"
        "    except Exception:\n"
        "        broken.append(mod)\n"
        "print('\\n'.join(broken))\n"
    )
    try:
        result = subprocess.run(
            [str(venv_python), "-c", check_script],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=False,
            env=env,
        )
    except Exception as exc:
        logger.debug("lazy refresh import probe failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.debug(
            "lazy refresh import probe exited %s: %s",
            result.returncode,
            (result.stderr or "")[:200],
        )
        return None

    broken_modules = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    packages: list[str] = []
    seen: set[str] = set()
    for mod in broken_modules:
        pkg = _LAZY_REFRESH_REPAIR_PACKAGES.get(mod)
        if pkg and pkg not in seen:
            seen.add(pkg)
            packages.append(pkg)
    return packages


def _repair_broken_lazy_refresh_imports(
    install_cmd_prefix: list[str],
    packages: list[str],
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Force-reinstall ``packages`` and re-probe imports. Never raises."""
    if not packages:
        return True

    specs = _lazy_refresh_repair_specs(packages)
    try:
        _run_package_only_install(
            install_cmd_prefix + ["install", "--force-reinstall", *specs],
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("lazy refresh venv repair failed: %s", exc)
        return False

    after = _detect_broken_lazy_refresh_imports(install_cmd_prefix, env=env)
    # Indeterminate re-probe is not confirmed success.
    return after == []


def _repair_venv_via_import_probes(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Probe imports and force-reinstall any broken lazy-refresh packages.

    Uses real ``import`` checks (not distribution metadata) so a venv where
    METADATA remains but ``.py`` files were wiped mid-install is still
    detected (#57828). Package-only reinstall.

    Never raises. Returns one of:
      - ``"healthy"`` — probes ran and found nothing broken
      - ``"repaired"`` — probes found breakage and force-reinstall confirmed clean
      - ``"failed"`` — probes found breakage and repair did not confirm clean
      - ``"indeterminate"`` — probes could not run; do NOT treat as healthy
    """
    broken = _detect_broken_lazy_refresh_imports(install_cmd_prefix, env=env)
    if broken is None:
        print(
            "  ⚠ Import probes unavailable — cannot confirm venv package health."
        )
        return "indeterminate"
    if not broken:
        return "healthy"
    print(
        "  → Detected corrupted venv packages via import probes: "
        f"{', '.join(broken)}; repairing..."
    )
    if _repair_broken_lazy_refresh_imports(
        install_cmd_prefix, broken, env=env
    ):
        print("  ✓ Venv repair succeeded")
        return "repaired"
    manual = " ".join(
        shlex.quote(s) for s in _lazy_refresh_repair_specs(broken)
    )
    print("  ⚠ Venv repair incomplete. Run manually, then `son-of-anton update`:")
    print(
        f"    {' '.join(install_cmd_prefix)} install --force-reinstall {manual}"
    )
    return "failed"


def _install_python_dependencies_with_optional_fallback(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
    group: str = "all",
) -> None:
    """Install base deps plus as many optional extras as the environment supports.

    By default this targets ``.[all]``.
    """
    def _install(args: list[str]) -> None:
        _run_install_with_heartbeat(install_cmd_prefix + args, env=env)

    try:
        _install(["install", "-e", f".[{group}]"])
        _verify_console_scripts_installed(install_cmd_prefix, env=env)
        return
    except subprocess.CalledProcessError:
        print(
            "  ⚠ Optional extras failed, reinstalling base dependencies and retrying extras individually..."
        )

    _install(["install", "-e", "."])

    failed_extras: list[str] = []
    installed_extras: list[str] = []
    for extra in _load_installable_optional_extras(group=group):
        try:
            _install(["install", "-e", f".[{extra}]"])
            installed_extras.append(extra)
        except subprocess.CalledProcessError:
            failed_extras.append(extra)

    if installed_extras:
        print(
            f"  ✓ Reinstalled optional extras individually: {', '.join(installed_extras)}"
        )
    if failed_extras:
        print(
            f"  ⚠ Skipped optional extras that still failed: {', '.join(failed_extras)}"
        )

    # Belt-and-suspenders: verify every declared core dependency from
    # pyproject.toml's [project.dependencies] is actually importable in the
    # target venv. uv's incremental resolver has — in the wild — produced
    # partial installs where a newly added base dep (e.g. ``pathspec``)
    # silently fails to land on top of a half-stale venv, and the only
    # symptom is a downstream subprocess crashing with ModuleNotFoundError
    # hours later inside ``son-of-anton update``'s desktop-rebuild or skill-sync
    # stage. Reinstall with --reinstall to force resolution if anything is
    # missing, then re-verify so the failure surfaces here instead of
    # downstream.
    _verify_core_dependencies_installed(install_cmd_prefix, env=env, group=group)
    _verify_console_scripts_installed(install_cmd_prefix, env=env)


def _verify_console_scripts_installed(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Windows-only entry-point shim verification; no-op on Nix platforms.

    Kept importable: ``update_cmd`` calls it after dependency installs.
    """
    return None


def _verify_core_dependencies_installed(
    install_cmd_prefix: list[str],
    *,
    env: dict[str, str] | None = None,
    group: str = "all",
) -> None:
    """Check that every base dep from pyproject.toml is importable; if not, retry.

    Reads ``pyproject.toml`` directly (so we don't trust the venv's stale
    metadata), filters out deps gated by ``;`` environment markers that don't
    apply to this platform, and runs ``importlib.metadata.version()`` in the
    venv interpreter for each one. If anything is missing we reinstall the
    base group with ``--reinstall`` to force uv to re-resolve, then check
    again. We treat the final state as a warning rather than a hard failure
    so a single broken-on-PyPI dep can't block an otherwise-successful
    update — but the warning makes the partial install visible at the spot
    that caused it, instead of hours later in a downstream subprocess.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover — Python < 3.11 unsupported but be safe
        return

    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return

    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        raw_deps = data.get("project", {}).get("dependencies", []) or []
    except Exception as e:
        logger.debug("dep verification: failed to read pyproject.toml: %s", e)
        return

    # Parse each "name OP version ; marker" string into (dist_name, marker_obj).
    # We use packaging.requirements when available (it ships with pip/uv envs),
    # falling back to a naive split that's good enough for the canonical
    # ``name==version[; marker]`` style this repo uses.
    deps: list[tuple[str, "object | None"]] = []
    try:
        from packaging.requirements import Requirement  # type: ignore

        for spec in raw_deps:
            try:
                req = Requirement(spec)
                deps.append((req.name, req.marker))
            except Exception:
                continue
    except Exception:
        for spec in raw_deps:
            head = spec.split(";", 1)[0]
            for op in ("==", ">=", "<=", "~=", ">", "<", "!="):
                if op in head:
                    head = head.split(op, 1)[0]
                    break
            name = head.strip().split("[", 1)[0].strip()
            if name:
                deps.append((name, None))

    # Apply environment markers to drop deps that don't apply on this platform
    # (e.g. ``ptyprocess ; sys_platform != 'win32'`` evaluates True here).
    # Without markers we'd false-positive every cross-platform exclusion.
    applicable: list[str] = []
    for name, marker in deps:
        if marker is None:
            applicable.append(name)
            continue
        try:
            if marker.evaluate():  # type: ignore[union-attr]
                applicable.append(name)
        except Exception:
            applicable.append(name)

    if not applicable:
        return

    # Run the check inside the venv Python — sys.executable here may be the
    # outer Python that drove ``son-of-anton update``, not the venv we just wrote
    # to. The uv install_cmd_prefix encodes which environment we targeted
    # (either ``[uv, pip]`` with VIRTUAL_ENV in env, or
    # ``[sys.executable, -m, pip]`` for the in-process Python); resolve the
    # right interpreter for the verification.
    venv_python = _resolve_install_target_python(install_cmd_prefix, env)
    if venv_python is None:
        return

    def _missing_deps() -> list[str]:
        check_script = (
            "import importlib.metadata as md, sys\n"
            "missing=[]\n"
            "for name in sys.argv[1:]:\n"
            "    try: md.version(name)\n"
            "    except md.PackageNotFoundError: missing.append(name)\n"
            "print('\\n'.join(missing))\n"
        )
        try:
            result = subprocess.run(
                [str(venv_python), "-c", check_script, *applicable],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                check=False,
                env=env,
            )
        except Exception as e:
            logger.debug("dep verification: subprocess failed: %s", e)
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    missing = _missing_deps()
    if not missing:
        return

    print(
        f"  ⚠ Verification: {len(missing)} declared dep(s) missing after install: "
        f"{', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}"
    )
    print("  → Reinstalling base group with --reinstall to repair...")

    # Reinstall base group with --reinstall so uv re-resolves from scratch
    # against the current pyproject. We don't pass ``[{group}]`` here on
    # purpose — the missing dep is in *base* deps; rerunning the full all-
    # extras install can cost minutes and trips on whatever optional extra
    # was already broken upstream. Base is fast and is what's actually wrong.
    repair_args = ["install", "--reinstall", "-e", "."]
    try:
        _run_install_with_heartbeat(install_cmd_prefix + repair_args, env=env)
    except subprocess.CalledProcessError as e:
        logger.warning("dep verification: repair install failed: %s", e)
        print("  ⚠ Repair install failed; check `son-of-anton update` output above.")
        return

    still_missing = _missing_deps()
    if not still_missing:
        print("  ✓ All declared core dependencies now installed")
        return

    # Last-ditch: install each remaining missing dep with its pin directly.
    # Useful when uv's resolver thinks the env is satisfied but the on-disk
    # package metadata says otherwise (rare but observed).
    name_to_spec = {}
    for spec in raw_deps:
        head = spec.split(";", 1)[0].strip()
        bare = head
        for op in ("==", ">=", "<=", "~=", ">", "<", "!="):
            if op in bare:
                bare = bare.split(op, 1)[0]
                break
        name_to_spec[bare.strip().split("[", 1)[0].strip()] = head

    specs = [name_to_spec.get(n, n) for n in still_missing]
    print(
        f"  → Force-installing remaining missing dep(s): {', '.join(specs)}"
    )
    try:
        _run_install_with_heartbeat(
            install_cmd_prefix + ["install", "--reinstall", *specs], env=env
        )
    except subprocess.CalledProcessError as e:
        logger.warning("dep verification: per-package repair failed: %s", e)
        print(
            f"  ⚠ Could not install: {', '.join(still_missing)}. "
            "Run `son-of-anton update --force` after closing other son-of-anton processes."
        )
        return

    final_missing = _missing_deps()
    if final_missing:
        print(
            f"  ⚠ Still missing after repair: {', '.join(final_missing)}. "
            "Run `son-of-anton update --force` after closing other son-of-anton processes."
        )
    else:
        print("  ✓ All declared core dependencies now installed")


def _resolve_install_target_python(
    install_cmd_prefix: list[str], env: dict[str, str] | None
) -> Path | None:
    """Figure out which Python interpreter the install just targeted.

    ``_install_python_dependencies_with_optional_fallback`` is called with
    either ``[uv, pip]`` (and a ``VIRTUAL_ENV`` env var pointing at the
    target venv) or ``[sys.executable, -m, pip]`` (the in-process Python).
    The verification step needs the *resulting* environment's Python so
    ``importlib.metadata`` queries the right site-packages.
    """
    if env and "VIRTUAL_ENV" in env:
        from son_of_anton_constants import venv_python_path

        venv_root = Path(env["VIRTUAL_ENV"])
        candidate = venv_python_path(venv_root)
        if candidate.exists():
            return candidate

    # Fallback: assume install_cmd_prefix[0] is the python interpreter (the
    # ``[sys.executable, -m, pip]`` shape). Skip if it looks like ``uv``.
    if install_cmd_prefix:
        first = Path(install_cmd_prefix[0])
        if first.exists() and "uv" not in first.name.lower():
            return first

    return None


def _resolve_node_runtime_npm() -> str | None:
    """Resolve an npm executable that belongs to the host's Node runtime."""
    from son_of_anton_constants import find_node_executable

    return find_node_executable("npm")


class _UpdateOutputStream:
    """Stream wrapper used during ``son-of-anton update`` to survive terminal loss.

    Wraps the process's original stdout/stderr so that:

    * Every write is also mirrored to an append-only log file
      (``~/.son-of-anton/logs/update.log``) that users can inspect after the
      terminal disconnects.
    * Writes to the original stream that fail with ``BrokenPipeError`` /
      ``OSError`` / ``ValueError`` (closed file) no longer cascade into
      process exit — the update keeps going, only the on-screen output
      stops.

    Combined with ``SIGHUP -> SIG_IGN`` installed by
    ``_install_hangup_protection``, this makes ``son-of-anton update`` safe to
    run in a plain SSH session that might disconnect mid-install.
    """

    def __init__(self, original, log_file):
        self._original = original
        self._log = log_file
        self._original_broken = False

    def write(self, data):
        # Mirror to the log file first — it's the most reliable destination.
        if self._log is not None:
            try:
                self._log.write(data)
            except Exception:
                # Log errors should never abort the update.
                pass

        if self._original_broken:
            return len(data) if isinstance(data, (str, bytes)) else 0

        try:
            return self._original.write(data)
        except (BrokenPipeError, OSError, ValueError):
            # Terminal vanished (SSH disconnect, shell close).  Stop trying
            # to write to it, but keep the update running.
            self._original_broken = True
            return len(data) if isinstance(data, (str, bytes)) else 0

    def flush(self):
        if self._log is not None:
            try:
                self._log.flush()
            except Exception:
                pass
        if self._original_broken:
            return
        try:
            self._original.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._original_broken = True

    def isatty(self):
        if self._original_broken:
            return False
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        # Some tools probe fileno(); defer to the underlying stream and let
        # callers handle failures (same behaviour as the unwrapped stream).
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_hangup_protection(gateway_mode: bool = False):
    """Protect ``cmd_update`` from SIGHUP and broken terminal pipes.

    Users commonly run ``son-of-anton update`` in an SSH session or a terminal
    that may close mid-install.  Without protection, ``SIGHUP`` from the
    terminal kills the Python process during ``pip install`` and leaves
    the venv half-installed; the documented workaround ("use screen /
    tmux") shouldn't be required for something as routine as an update.

    Protections installed:

    1. ``SIGHUP`` is set to ``SIG_IGN``.  POSIX preserves ``SIG_IGN``
       across ``exec()``, so pip and git subprocesses also stop dying on
       hangup.
    2. ``sys.stdout`` / ``sys.stderr`` are wrapped to mirror output to
       ``~/.son-of-anton/logs/update.log`` and to silently absorb
       ``BrokenPipeError`` when the terminal vanishes.

    ``SIGINT`` (Ctrl-C) and ``SIGTERM`` (systemd shutdown) are
    **intentionally left alone** — those are legitimate cancellation
    signals the user or OS sent on purpose.

    In gateway mode (``son-of-anton update --gateway``) the update is already
    spawned detached from a terminal, so this function is a no-op.

    Returns a dict that ``cmd_update`` can pass to
    ``_finalize_update_output`` on exit.  Returning a dict rather than a
    tuple keeps the call site forward-compatible with future additions.
    """
    state = {
        "prev_stdout": sys.stdout,
        "prev_stderr": sys.stderr,
        "log_file": None,
        "installed": False,
    }

    if gateway_mode:
        return state

    import signal as _signal

    # (1) Ignore SIGHUP for the remainder of this process.
    if hasattr(_signal, "SIGHUP"):
        try:
            _signal.signal(_signal.SIGHUP, _signal.SIG_IGN)
        except (ValueError, OSError):
            # Called from a non-main thread — not fatal.  The update still
            # runs, just without hangup protection.
            pass

    # (2) Mirror output to update.log and wrap stdio for broken-pipe
    # tolerance.  Any failure here is non-fatal; we just skip the wrap.
    try:
        # Late-bound import so tests can monkeypatch
        # son_of_anton_cli.config.get_son_of_anton_home to simulate setup failure.
        from son_of_anton_cli.config import get_son_of_anton_home as _get_son_of_anton_home

        logs_dir = _get_son_of_anton_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "update.log"
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")

        import datetime as _dt

        log_file.write(
            f"\n=== son-of-anton update started "
            f"{_dt.datetime.now().isoformat(timespec='seconds')} ===\n"
        )

        state["log_file"] = log_file
        sys.stdout = _UpdateOutputStream(state["prev_stdout"], log_file)
        sys.stderr = _UpdateOutputStream(state["prev_stderr"], log_file)
        state["installed"] = True
    except Exception:
        # Leave stdio untouched on any setup failure.  Update continues
        # without mirroring.
        state["log_file"] = None

    return state


def _finalize_update_output(state):
    """Restore stdio and close the update.log handle opened by ``_install_hangup_protection``."""
    if not state:
        return
    if state.get("installed"):
        try:
            sys.stdout = state.get("prev_stdout", sys.stdout)
        except Exception:
            pass
        try:
            sys.stderr = state.get("prev_stderr", sys.stderr)
        except Exception:
            pass
    log_file = state.get("log_file")
    if log_file is not None:
        try:
            log_file.flush()
            log_file.close()
        except Exception:
            pass


def _resolve_update_branch(args) -> str:
    """Normalize ``args.branch`` into a non-empty branch name.

    Centralizes the "default to main, accept --branch override, treat empty
    or whitespace-only values as the default" parsing so every consumer of
    ``--branch`` (check path, git-update path, ZIP-fallback path) agrees on
    the same answer.
    """
    return (getattr(args, "branch", None) or "main").strip() or "main"


def _size_delta_label(saved_mb: float) -> str:
    """Human label for a before/after database size delta, in MB.

    A negative delta means the file GREW — concurrent session writes during a
    long optimize can outweigh what the rebuild freed. Printing
    "reclaimed -163.0 MB" for that reads as data loss, so say "grew by"
    instead.
    """
    if saved_mb >= 0:
        return f"reclaimed {saved_mb:.1f} MB"
    return f"grew by {-saved_mb:.1f} MB"


def _coalesce_session_name_args(argv: list) -> list:
    """Join unquoted multi-word session names after -c/--continue and -r/--resume.

    When a user types ``son-of-anton -c Pokemon Agent Dev`` without quoting the
    session name, argparse sees three separate tokens.  This function merges
    them into a single argument so argparse receives
    ``['-c', 'Pokemon Agent Dev']`` instead.

    Tokens are collected after the flag until we hit another flag (``-*``)
    or a known top-level subcommand.
    """
    # Exactly the top-level subcommands this fork registers. A name in here
    # that is not a real command is not harmless: it ends the session name
    # early, so `son-of-anton -c auth notes` would lose everything from "auth"
    # on. This list carried eleven commands the fork does not have.
    _SUBCOMMANDS = {
        "chat",
        "completion",
        "config",
        "cron",
        "gateway",
        "mcp",
        "model",
        "pause",
        "problem",
        "resume",
        "sessions",
        "skills",
        "status",
    }
    _SESSION_FLAGS = {"-c", "--continue", "-r", "--resume"}

    result = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _SESSION_FLAGS:
            result.append(token)
            i += 1
            # Collect subsequent non-flag, non-subcommand tokens as one name
            parts: list = []
            while (
                i < len(argv)
                and not argv[i].startswith("-")
                and argv[i] not in _SUBCOMMANDS
            ):
                parts.append(argv[i])
                i += 1
            if parts:
                result.append(" ".join(parts))
        else:
            result.append(token)
            i += 1
    return result


def cmd_problem(args, parser=None):
    """Create or run a physics problem spec."""
    action = getattr(args, "problem_action", None)

    if action == "create":
        from physics_intern.spec_builder import run as run_spec_builder

        return run_spec_builder(args, parser)

    if action == "run":
        from physics_intern.run import render_report, run_problem

        mode = getattr(args, "mode", "physics")
        workspace = run_problem(
            args.spec,
            mode=mode,
            max_iterations=getattr(args, "max_iterations", None),
            script_timeout=getattr(args, "script_timeout", None),
            workspace_root=getattr(args, "workspace", None),
        )
        print(render_report(workspace, mode))
        return 0

    print(
        "usage: son-of-anton problem create --data PATH --goal TEXT -o FILE\n"
        "       son-of-anton problem run SPEC [--mode physics|research]\n"
        "Run 'son-of-anton problem <action> --help' for the full options."
    )
    return 2


def cmd_completion(args, parser=None):
    """Print shell completion script."""
    from son_of_anton_cli.completion import generate_bash, generate_zsh, generate_fish

    shell = getattr(args, "shell", "bash")
    if shell == "zsh":
        print(generate_zsh(parser))
    elif shell == "fish":
        print(generate_fish(parser))
    else:
        print(generate_bash(parser))


def _build_provider_choices() -> list[str]:
    """Build the --provider choices list from CANONICAL_PROVIDERS + 'auto'."""
    try:
        from son_of_anton_cli.models import CANONICAL_PROVIDERS as _cp
        return ["auto"] + [p.slug for p in _cp]
    except Exception:
        # Fallback: static list guarantees the CLI always works
        return ["auto", "openai-api", "custom"]


# Top-level subcommands that argparse knows about WITHOUT running plugin
# discovery.  Used to short-circuit eager plugin imports (which can take
# 500ms+ pulling in google.cloud.pubsub_v1, aiohttp, grpc, etc.) when the
# user's invocation clearly doesn't need any plugin-registered subcommand.
#
# Keep this in sync with the ``subparsers.add_parser("NAME", ...)`` calls
# below in ``main()``. Missing an entry here only costs a one-time
# discovery; extra entries here would let a plugin command silently fail
# to parse.
_BUILTIN_SUBCOMMANDS = frozenset(
    {
        "chat", "completion", "config", "cron", "gateway", "mcp", "model",
        "pause", "problem", "resume", "sessions", "skills", "status",
        # Help-ish invocations — plugin commands not being listed in
        # top-level --help is an acceptable trade-off for skipping an
        # expensive eager import of every bundled plugin module.
        "help",
    }
)


# Top-level flags that take a value. Needed by ``_first_positional_argv``
# so that in ``son-of-anton -m gpt5 chat``, ``gpt5`` is correctly skipped as a
# flag value rather than misclassified as a subcommand. Kept in sync with
# the top-level flags declared in ``son_of_anton_cli/_parser.py``.
#
# Correctness-safe either way: missing an entry here only makes the
# fast-path bail out too eagerly (we run plugin discovery when we didn't
# need to); extra entries would make us skip a real positional.
_TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "-z", "--oneshot",
        "-m", "--model",
        "--provider",
        "-t", "--toolsets",
        "-r", "--resume",
        "-s", "--skills",
        "--usage-file",
        "--in",
        # ``-c / --continue`` is nargs='?' (optional value). Treat it as
        # value-taking: if the next token is a subcommand-looking word
        # the user almost certainly meant it as the session name, and
        # either interpretation keeps us on the safe side.
        "-c", "--continue",
    }
)


def _first_positional_argv() -> str | None:
    """Return the first non-flag, non-flag-value token in ``sys.argv[1:]``.

    Used by ``main()`` to decide whether plugin discovery has to run at
    argparse-setup time. Handles common invocations like
    ``son-of-anton -m gpt5 --provider openai chat "msg"`` by skipping the
    values attached to known top-level flags.

    Does NOT fully simulate argparse — unknown ``--foo=bar`` / ``--foo
    bar`` flags degrade gracefully (``bar`` may be wrongly classified as
    a positional, which at worst forces a one-time plugin discovery).
    """
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # Everything after ``--`` is positional.
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("-"):
            # ``--flag=value`` carries its value inline — single token.
            if "=" in tok:
                i += 1
                continue
            if tok in _TOP_LEVEL_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        return tok
    return None


def _plugin_cli_discovery_needed() -> bool:
    """True when the CLI might be invoking a plugin-registered subcommand.

    Returning False lets ``main()`` skip plugin discovery entirely during
    argparse setup, saving ~500-650ms per invocation for users whose
    enabled plugins don't contribute any CLI command.
    """
    first = _first_positional_argv()
    if first is None:
        # Bare ``son-of-anton`` or only flags → defaults to ``chat``.
        return False
    if first in _BUILTIN_SUBCOMMANDS:
        return False
    # Unknown token — could be a plugin subcommand, OR a chat prompt
    # starting with a non-flag word. Either way we need discovery: if it
    # IS a plugin command, argparse needs the subparser; if it's a chat
    # prompt, argparse will route it via positional handling and the
    # extra discovery cost is amortized over a full agent run anyway.
    return True


def _resolve_deferred_platform_cli_command(command_name: str | None) -> None:
    """Materialize the deferred platform whose top-level CLI command matches.

    Bundled platform plugins are cheap-registered as *deferred* entries to
    avoid importing every gateway SDK during normal startup. A platform that
    registers a top-level ``son-of-anton <name>`` command
    (``ctx.register_cli_command(name=..., ...)``) only runs that side
    effect when its module is imported. On the unknown-top-level-command slow
    path, ``discover_plugins()`` records the deferred loader but does not
    import it, so the CLI registration never happens and the CLI command is
    unavailable.
    fails with argparse ``invalid choice`` (issue #54678).

    Resolving only the platform whose name matches the first positional token
    keeps normal startup cheap while making the targeted command available.
    """
    if not command_name:
        return
    try:
        from gateway.platform_registry import platform_registry

        platform_registry.get(command_name)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Deferred platform CLI resolution failed for %s: %s",
            command_name,
            exc,
        )


_AGENT_COMMANDS = {None, "chat", "rl"}
_AGENT_SUBCOMMANDS = {
    "cron": ("cron_command", {"run", "tick"}),
    "gateway": ("gateway_command", {"run"}),
    "mcp": ("mcp_action", {"serve"}),
}


def _command_has_dedicated_mcp_startup(args) -> bool:
    if args.command == "gateway" and getattr(args, "gateway_command", None) == "run":
        return True
    if args.command == "cron" and getattr(args, "cron_command", None) in {"run", "tick"}:
        return True
    return False


def _should_background_mcp_startup(args) -> bool:
    return args.command in {None, "chat", "rl"}


def _prepare_agent_startup(args) -> None:
    """Discover plugins/MCP/hooks for commands that can run an agent turn."""
    # --yolo: chokepoint guarantee that SON_OF_ANTON_YOLO_MODE is set before ANY
    # plugin/tool discovery below imports tools.approval, which freezes
    # _YOLO_MODE_FROZEN at import time (PR #7994 security design).  main()'s
    # dispatch path also sets this earlier, but _prepare_agent_startup() is
    # reachable from other launchers too, so the guarantee lives here where
    # the import is actually triggered (#60328).
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"
    _apply_safe_mode(args)

    _sub_attr, _sub_set = _AGENT_SUBCOMMANDS.get(args.command, (None, None))
    if not (
        args.command in _AGENT_COMMANDS
        or (_sub_attr and getattr(args, _sub_attr, None) in _sub_set)
    ):
        return

    _accept_hooks = bool(getattr(args, "accept_hooks", False))
    try:
        from son_of_anton_cli.plugins import start_background_plugin_discovery

        # Discovery runs in a daemon thread so its ~150ms of manifest
        # scanning + plugin imports overlaps the rest of startup (cli /
        # prompt_toolkit imports, worktree git calls). Correctness is
        # unchanged: every synchronous reader goes through
        # discover_plugins(), which joins this thread first — including
        # the discover_plugins() call model_tools makes at import time,
        # which happens before any tool list is built.
        start_background_plugin_discovery()
    except Exception:
        logger.warning(
            "plugin discovery failed at CLI startup",
            exc_info=True,
        )
    _run_inline_mcp_discovery = True
    if _command_has_dedicated_mcp_startup(args):
        # These entrypoints already do their own MCP startup later on the real
        # runtime path (gateway executor, ACP launcher, cron job runner).
        _run_inline_mcp_discovery = False
    elif _should_background_mcp_startup(args):
        try:
            from son_of_anton_cli.mcp_startup import start_background_mcp_discovery

            start_background_mcp_discovery(
                logger=logger,
                thread_name="cli-mcp-discovery",
            )
        except Exception:
            logger.debug(
                "Background MCP tool discovery failed at CLI startup",
                exc_info=True,
            )
        _run_inline_mcp_discovery = False
    if _run_inline_mcp_discovery:
        try:
            # MCP tool discovery remains synchronous for entrypoints that do
            # not own a later bounded/executor startup path.
            from tools.mcp_tool import discover_mcp_tools

            discover_mcp_tools()
        except Exception:
            logger.debug(
                "MCP tool discovery failed at CLI startup",
                exc_info=True,
            )
    try:
        from son_of_anton_cli.config import load_config
        from agent.shell_hooks import register_from_config

        _hooks_cfg = load_config()
        register_from_config(_hooks_cfg, accept_hooks=_accept_hooks)

        from agent.outbound_webhooks import (
            register_from_config as register_outbound_webhooks,
        )

        register_outbound_webhooks(_hooks_cfg)
    except Exception:
        logger.debug(
            "shell-hook registration failed at CLI startup",
            exc_info=True,
        )


def _apply_safe_mode(args) -> None:
    if not getattr(args, "safe_mode", False):
        return
    os.environ["SON_OF_ANTON_SAFE_MODE"] = "1"
    os.environ["SON_OF_ANTON_IGNORE_USER_CONFIG"] = "1"
    os.environ["SON_OF_ANTON_IGNORE_RULES"] = "1"


def _set_chat_arg_defaults(args) -> None:
    for attr, default in [
        ("query", None),
        ("model", None),
        ("provider", None),
        ("toolsets", None),
        ("verbose", False),
        ("resume", None),
        ("continue_last", None),
        ("worktree", False),
    ]:
        if not hasattr(args, attr):
            setattr(args, attr, default)


def _try_fast_chat_launch() -> bool:
    """Fast path for unambiguous interactive chat launches (all hosts).

    ``son-of-anton`` / ``son-of-anton -w -s foo --yolo`` / ``son-of-anton chat`` don't need the
    full argparse tree: building all ~40 subcommand parsers costs ~140ms of
    pure-Python argparse setup plus their module imports, none of which the
    chat path uses. Parse the lightweight top-level/chat parser instead and
    dispatch straight to ``cmd_chat``.

    Bails out (returns False) whenever the invocation is not certainly a
    chat launch — a subcommand positional, ``--help``, unknown flags — so
    every other path still goes through the full parser unchanged.
    """
    if os.environ.get("SON_OF_ANTON_DISABLE_FAST_CHAT_LAUNCH") == "1":
        return False
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        return False
    if _first_positional_argv() not in {None, "chat"}:
        return False

    from son_of_anton_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=cmd_chat)
    try:
        args, unknown = parser.parse_known_args(_coalesce_session_name_args(argv))
    except SystemExit:
        return False
    if unknown:
        # Flags the light parser doesn't know — could belong to a plugin
        # subcommand or a newer full-parser flag. Fall back to full dispatch.
        return False
    if getattr(args, "version", False):
        return False
    if getattr(args, "command", None) not in {None, "chat"}:
        return False

    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"
    _prepare_agent_startup(args)

    if getattr(args, "oneshot", None):
        _confirm_startup_expensive_model_override(args)
        _run_and_exit_oneshot(
            args.oneshot,
            model=getattr(args, "model", None),
            provider=getattr(args, "provider", None),
            toolsets=getattr(args, "toolsets", None),
            skills=getattr(args, "skills", None),
            usage_file=getattr(args, "usage_file", None),
        )

    if (args.resume or args.continue_last) and args.command is None:
        args.command = "chat"

    _set_chat_arg_defaults(args)
    cmd_chat(args)
    return True


def cmd_skills(args):
    # Route 'config' action to skills_config module
    if getattr(args, "skills_action", None) == "config":
        _require_tty("skills config")
        from son_of_anton_cli.skills_config import skills_command as skills_config_command

        skills_config_command(args)
    elif getattr(args, "skills_action", None) in ("trust", "untrust"):
        _cmd_skills_trust(args)
    else:
        from son_of_anton_cli.skills_hub import skills_command

        skills_command(args)


def _cmd_skills_trust(args):
    """``son-of-anton skills trust [path]`` / ``son-of-anton skills untrust [path]``.

    Manages ``skills.trusted_project_dirs`` in config.yaml. With no path,
    operates on the project root enclosing the current directory (nearest
    ancestor with ``.git``).
    """
    from pathlib import Path

    from agent.skill_utils import (
        PROJECT_SKILLS_SUBDIRS,
        _candidate_project_skills_dirs,
        find_project_root,
        iter_skill_index_files,
    )
    from son_of_anton_cli.config import load_config, save_config

    action = args.skills_action
    raw_path = getattr(args, "path", None)
    if raw_path:
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            print(f"Not a directory: {root}")
            return
    else:
        root = find_project_root()
        if root is None:
            print(
                "Not inside a git checkout. Run from a project directory or "
                "pass the project root path explicitly."
            )
            return

    config = load_config()
    skills_cfg = config.setdefault("skills", {})
    trusted = skills_cfg.get("trusted_project_dirs") or []
    if not isinstance(trusted, list):
        trusted = [trusted]
    trusted = [str(t) for t in trusted]
    root_str = str(root)

    if action == "untrust":
        kept = [t for t in trusted if str(Path(t).expanduser().resolve()) != root_str]
        if len(kept) == len(trusted):
            print(f"{root} was not trusted.")
            return
        skills_cfg["trusted_project_dirs"] = kept
        save_config(config)
        print(f"Untrusted: {root}")
        print("Project skills from this repo will no longer load.")
        return

    # trust
    if any(str(Path(t).expanduser().resolve()) == root_str for t in trusted):
        print(f"Already trusted: {root}")
    else:
        trusted.append(root_str)
        skills_cfg["trusted_project_dirs"] = trusted
        save_config(config)
        print(f"Trusted: {root}")

    # Show what this unlocks
    count = 0
    for d in _candidate_project_skills_dirs(root):
        count += sum(1 for _ in iter_skill_index_files(d, "SKILL.md"))
    if count:
        print(
            f"{count} project skill(s) will load in sessions started inside "
            "this repo (they take precedence over same-named profile skills)."
        )
    else:
        subdirs = " or ".join(PROJECT_SKILLS_SUBDIRS)
        print(f"No project skills found yet — add them under {subdirs}.")


def cmd_mcp(args):
    from son_of_anton_cli.mcp_config import mcp_command

    mcp_command(args)


def _advertise_agent_env() -> None:
    """Advertise the agent harness to child processes.

    ``AI_AGENT`` is the emerging cross-agent standard (huggingface_hub's agent
    detection reads it; pi and other agents set it — earendil-works/pi#7493)
    so generic tooling can attribute subprocesses to the harness that spawned
    them. The value must be our id in the public agent-harness registry
    (``son-of-anton`` in huggingface.js ``agent-harnesses.ts``): standard-var
    matching is exact, so any other value is counted as "unknown".
    ``SON_OF_ANTON_AGENT`` is the Son of Anton-specific marker. setdefault: never
    clobber an outer harness (e.g. Son of Anton running inside another agent's
    terminal).
    """
    os.environ.setdefault("AI_AGENT", "son-of-anton")
    os.environ.setdefault("SON_OF_ANTON_AGENT", "true")


class _CwdEnvTrace(dict):
    """Debug wrapper that logs every TERMINAL_* os.environ write with a stack."""

    def __setitem__(self, key, value):
        if isinstance(key, str) and key.startswith("TERMINAL_"):
            old = dict.get(self, key)
            if old != value:
                import logging
                import traceback
                logging.getLogger("son_of_anton_cwd_debug").info(
                    "ENV WRITE %s: %r -> %r @ %s",
                    key, old, value,
                    " | ".join(
                        line.strip()
                        for line in traceback.format_stack(limit=9)[:-1]
                    ),
                )
        return dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if isinstance(key, str) and key.startswith("TERMINAL_"):
            import logging
            import traceback
            logging.getLogger("son_of_anton_cwd_debug").info(
                "ENV DELETE %s: %r @ %s",
                key, dict.get(self, key),
                " | ".join(
                    line.strip()
                    for line in traceback.format_stack(limit=9)[:-1]
                ),
            )
        return dict.__delitem__(self, key)


def _install_cwd_env_tracer() -> None:
    """Replace os.environ with a write-tracing proxy (DEBUG_CWD gate only)."""
    import os as _os
    _os.environ = _CwdEnvTrace(_os.environ)


def _guard_son_of_anton_home_access() -> None:
    """Fail fast with a clear message when SON_OF_ANTON_HOME is unusable.

    A stale/cross-user home (plain ``su`` without ``-`` carries
    ``SON_OF_ANTON_HOME`` from the other account) makes every home path
    PermissionError: .env, config.yaml, .managed, plugins/, sessions. We
    cannot run in another account's 0700 home, so instead of a raw
    traceback from whichever module hits the wall first, one message
    naming the actual problem and the fix. ``--help`` / ``--version`` stay
    usable. A missing home is NOT guarded (first run; setup creates it).
    """
    try:
        home = get_son_of_anton_home()
        home.stat()
        if not os.access(home, os.R_OK | os.X_OK):
            raise PermissionError(f"home {home} is not readable/executable by this user")
    except FileNotFoundError:
        return  # first run — setup will create it
    except PermissionError as exc:
        if "-h" in sys.argv[1:] or "--help" in sys.argv[1:] or "--version" in sys.argv[1:]:
            return
        print(
            "error: SON_OF_ANTON_HOME points at a directory this user cannot access — "
            f"{get_son_of_anton_home()!s}\n"
            f"  {exc}\n"
            "This usually means the variable leaked across accounts (e.g. `su` "
            "without `-`, or a profile export). Fix:\n"
            "  su - <user>       # login shell resets the environment\n"
            "  unset SON_OF_ANTON_HOME   # use this account's default home\n"
            "  export SON_OF_ANTON_HOME=$HOME/.son-of-anton  # correct home",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    """Main entry point for son-of-anton CLI."""
    _guard_son_of_anton_home_access()
    if os.environ.get("SON_OF_ANTON_DEBUG_CWD"):
        _install_cwd_env_tracer()

    # Cosmetic: make the process show up as 'son-of-anton' instead of 'python3.11'
    # in ps/top/htop.  Non-fatal — just a nicer UX.
    _set_process_title()

    # Let child processes (and tools like huggingface_hub) detect they run
    # under an AI agent harness.
    _advertise_agent_env()

    # If the checkout changed since the last launch (son-of-anton update, manual
    # git pull, old-updater update that predates newer clears), sweep stale
    # __pycache__ once so no process — this one's lazy imports included —
    # resolves fresh source against old bytecode. Never raises.
    _sweep_stale_bytecode_if_checkout_changed()

    # Self-heal a venv left half-built by an interrupted ``son-of-anton update``
    # (Ctrl-C, terminal close, WSL OOM mid-install). Skip when the user is
    # *running* update — that flow writes and clears its own marker, and we
    # don't want a recovery install racing the real one. Never raises.
    #
    # The substring match is deliberately loose: argv isn't parsed yet at this
    # point, and the failure modes are asymmetric. Over-matching (e.g.
    # ``son-of-anton skills install update``) merely defers recovery one launch;
    # under-matching (missing ``son-of-anton -p work update``) would race a recovery
    # install against the real one. Loose wins.
    try:
        if "update" not in sys.argv[1:]:
            _recover_from_interrupted_install()
    except Exception:
        pass

    if _try_fast_chat_launch():
        return

    from son_of_anton_cli._parser import build_top_level_parser

    parser, subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=cmd_chat)

    # =========================================================================
    # model command  (parser built in son_of_anton_cli/subcommands/model.py)
    # =========================================================================
    build_model_parser(subparsers, cmd_model=cmd_model)
    # =========================================================================
    # gateway + proxy commands  (parsers built in son_of_anton_cli/subcommands/gateway.py)
    # =========================================================================
    build_gateway_parser(subparsers, cmd_gateway=cmd_gateway)

    # =========================================================================
    # status command  (parser built in son_of_anton_cli/subcommands/status.py)
    # =========================================================================
    build_status_parser(subparsers, cmd_status=cmd_status)

    # =========================================================================
    # pause / resume commands  (parser built in son_of_anton_cli/subcommands/pause.py)
    # =========================================================================
    build_pause_parser(subparsers)

    # =========================================================================
    # cron command  (parser built in son_of_anton_cli/subcommands/cron.py)
    # =========================================================================
    build_cron_parser(subparsers, cmd_cron=cmd_cron)

    # =========================================================================
    # config command  (parser built in son_of_anton_cli/subcommands/config.py)
    # =========================================================================
    build_config_parser(subparsers, cmd_config=cmd_config)

    # =========================================================================
    # skills command  (parser built in son_of_anton_cli/subcommands/skills.py)
    # =========================================================================
    build_skills_parser(subparsers, cmd_skills=cmd_skills)

    # =========================================================================
    # Plugin CLI commands — dynamically registered by memory/general plugins.
    # Plugins provide a register_cli(subparser) function that builds their
    # own argparse tree.  No hardcoded plugin commands in main.py.
    #
    # Skipped when the invocation is already targeting a known built-in
    # subcommand — ``son-of-anton --help``, ``son-of-anton logs``,
    # etc.  This avoids eagerly importing every bundled plugin module
    # (google.cloud.pubsub_v1, aiohttp, grpc, PIL …) which costs
    # 500-650ms on typical installs.
    # =========================================================================
    if _plugin_cli_discovery_needed():
        try:
            from plugins.memory import discover_plugin_cli_commands
            from son_of_anton_cli.plugins import discover_plugins, get_plugin_manager

            seen_plugin_commands = set()
            for cmd_info in discover_plugin_cli_commands():
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
                seen_plugin_commands.add(cmd_info["name"])

            discover_plugins()
            # A bundled platform whose top-level CLI command is the one being
            # invoked is still only a deferred entry at this point; import it
            # so its register_cli_command side effect runs before we read
            # _cli_commands (issue #54678).
            _resolve_deferred_platform_cli_command(_first_positional_argv())
            for cmd_info in get_plugin_manager()._cli_commands.values():
                if cmd_info["name"] in seen_plugin_commands:
                    continue
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
        except Exception as _exc:
            logging.getLogger(__name__).debug("Plugin CLI discovery failed: %s", _exc)

    # =========================================================================
    # mcp command  (parser built in son_of_anton_cli/subcommands/mcp.py)
    # =========================================================================
    build_mcp_parser(subparsers, cmd_mcp=cmd_mcp)
    build_problem_parser(subparsers, cmd_problem=cmd_problem)
    build_completion_parser(subparsers, parser, cmd_completion=cmd_completion)

    # =========================================================================
    # sessions command
    # =========================================================================
    sessions_parser = subparsers.add_parser(
        "sessions",
        help="Manage session history (list, rename, export, prune, delete)",
        description="View and manage the SQLite session store",
    )
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_action")

    sessions_list = sessions_subparsers.add_parser("list", help="List recent sessions")
    sessions_list.add_argument(
        "--source", help="Filter by source (cli, discord, slack, etc.)"
    )
    sessions_list.add_argument(
        "--limit", type=int, default=20, help="Max sessions to show"
    )
    sessions_list.add_argument(
        "--workspace",
        metavar="NEEDLE",
        help="Only sessions in one workspace: a git repo root or project dir "
        "(matched by path substring or basename).",
    )

    def _add_session_filter_args(p, default_older_help):
        p.add_argument(
            "--older-than",
            metavar="AGE",
            help=default_older_help,
        )
        p.add_argument(
            "--newer-than",
            metavar="AGE",
            help="Only match sessions active within the last AGE "
            "(e.g. '5h', '2d') or after an ISO timestamp",
        )
        p.add_argument(
            "--before",
            metavar="TIME",
            help="Only match sessions started before TIME "
            "(duration ago like '5h', or ISO timestamp like '2026-07-05 14:30')",
        )
        p.add_argument(
            "--after",
            metavar="TIME",
            help="Only match sessions started at/after TIME "
            "(duration ago like '5h', or ISO timestamp)",
        )
        p.add_argument("--source", help="Only match sessions from this source")
        p.add_argument(
            "--title", help="Only match sessions whose title contains this substring"
        )
        p.add_argument(
            "--end-reason", help="Only match sessions with this end reason"
        )
        p.add_argument(
            "--cwd", help="Only match sessions whose working directory is under this path"
        )
        p.add_argument(
            "--min-messages", type=int, help="Only match sessions with >= N messages"
        )
        p.add_argument(
            "--max-messages", type=int, help="Only match sessions with <= N messages"
        )
        p.add_argument(
            "--model",
            help="Only match sessions whose model name contains this substring "
            "(e.g. 'sonnet', 'gpt-5', 'son-of-anton')",
        )
        p.add_argument(
            "--provider",
            help="Only match sessions billed through this provider "
            "(e.g. openai-api, custom)",
        )
        p.add_argument(
            "--user", help="Only match sessions from this user ID"
        )
        p.add_argument(
            "--chat-id", help="Only match sessions from this chat/channel ID"
        )
        p.add_argument(
            "--chat-type",
            help="Only match sessions with this chat type (e.g. dm, group)",
        )
        p.add_argument(
            "--branch",
            help="Only match sessions whose git branch contains this substring",
        )
        p.add_argument(
            "--min-tokens", type=int,
            help="Only match sessions with >= N total tokens (input+output)",
        )
        p.add_argument(
            "--max-tokens", type=int,
            help="Only match sessions with <= N total tokens (input+output)",
        )
        p.add_argument(
            "--min-cost", type=float,
            help="Only match sessions costing >= N USD (actual or estimated)",
        )
        p.add_argument(
            "--max-cost", type=float,
            help="Only match sessions costing <= N USD (actual or estimated)",
        )
        p.add_argument(
            "--min-tool-calls", type=int,
            help="Only match sessions with >= N tool calls",
        )
        p.add_argument(
            "--max-tool-calls", type=int,
            help="Only match sessions with <= N tool calls",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="List matching sessions without changing anything",
        )
        p.add_argument(
            "--yes", "-y", action="store_true", help="Skip confirmation"
        )

    sessions_export = sessions_subparsers.add_parser(
        "export", help="Export sessions to JSONL, Markdown, or QMD"
    )
    sessions_export.add_argument(
        "output",
        nargs="?",
        help=(
            "Output path. JSONL: file path (use - for stdout, required). "
            "md/qmd: output directory (default: <son-of-anton home>/session-exports)"
        ),
    )
    sessions_export.add_argument(
        "--format",
        choices=["jsonl", "md", "qmd", "html", "trace"],
        default="jsonl",
        help=(
            "Export format (default: jsonl). 'trace' emits Claude Code JSONL "
            "for the Hugging Face Agent Trace Viewer"
        ),
    )
    sessions_export.add_argument(
        "--upload",
        action="store_true",
        help=(
            "trace only: upload to your Hugging Face traces dataset instead "
            "of writing a local file (needs HF_TOKEN)"
        ),
    )
    sessions_export.add_argument(
        "--public",
        action="store_true",
        help="trace --upload only: create/update a public dataset instead of private",
    )
    sessions_export.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "trace only: skip the forced secret redaction; "
            "only use after manual review"
        ),
    )
    sessions_export.add_argument(
        "--only",
        choices=["user-prompts"],
        help=(
            "Export only a filtered view (user-prompts: one prompt record "
            "per line for jsonl, headed sections for md)"
        ),
    )
    sessions_export.add_argument(
        "--session-id", help="Session ID or unique prefix to export"
    )
    _add_session_filter_args(
        sessions_export,
        "Only export sessions older than AGE (duration like '5h'/'2d', "
        "bare number of days, or an ISO timestamp)",
    )
    sessions_export.add_argument(
        "--redact",
        action="store_true",
        help="Redact secrets (API keys, tokens, credentials) from exported content",
    )
    sessions_export.add_argument(
        "--lineage",
        choices=["single", "logical"],
        default="single",
        help="md/qmd only: export one row or its compression lineage",
    )
    sessions_export.add_argument(
        "--delete-after-verified",
        action="store_true",
        help="md/qmd only: after verified single-session export, delete that session (needs --yes)",
    )
    sessions_export.add_argument(
        "--force",
        action="store_true",
        help="md/qmd only: overwrite an existing export file",
    )

    sessions_delete = sessions_subparsers.add_parser(
        "delete", help="Delete a specific session"
    )
    sessions_delete.add_argument("session_id", help="Session ID to delete")
    sessions_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )

    sessions_prune = sessions_subparsers.add_parser(
        "prune",
        help="Delete old sessions (filterable by time window, source, title, ...)",
    )
    _add_session_filter_args(
        sessions_prune,
        "Delete sessions older than AGE — days if bare number, or a duration "
        "like '5h'/'2d'/'1w', or an ISO timestamp (bare prune with no filters "
        "defaults to 90 days; any filter matches all ages)",
    )
    sessions_prune.add_argument(
        "--include-archived",
        action="store_true",
        help="Also delete archived sessions (excluded by default)",
    )
    sessions_prune.add_argument(
        "--include-pinned",
        action="store_true",
        help="Also delete pinned sessions (excluded by default — pin is a keep flag)",
    )
    sessions_prune.add_argument(
        "--never-active",
        action="store_true",
        help=(
            "Instead of ended sessions, delete keyed gateway rows that were "
            "opened and never used (no messages, tokens, tool calls or title) "
            "and are older than AGE (default 30 days). Ordinary prune can "
            "never reach these — it only ever selects ended sessions"
        ),
    )

    sessions_archive = sessions_subparsers.add_parser(
        "archive",
        help="Bulk-archive (soft-hide) sessions matching filters — no deletion",
    )
    _add_session_filter_args(
        sessions_archive,
        "Only archive sessions older than AGE (duration like '5h'/'2d', "
        "bare number of days, or ISO timestamp)",
    )

    sessions_subparsers.add_parser(
        "optimize",
        help="Reclaim disk space: merge FTS5 segments + VACUUM (no data change)",
    )

    sessions_clean_markers = sessions_subparsers.add_parser(
        "clean-markers",
        help="Permanently clear stale tool-call marker content left by sessions from before #78148",
        description=(
            "Before the #78148 fix, a local tool-call template could persist a "
            "bare bracketed marker (e.g. \"[memory]\") as an assistant turn's "
            "content instead of real text. This is already repaired in memory "
            "on every session load, so running this is optional — it rewrites "
            "the affected rows once, in place, so long-lived sessions stop "
            "re-scanning/re-repairing the same rows on every resume. Only the "
            "content column is touched; tool_calls and every other column on "
            "the row are left untouched."
        ),
    )
    sessions_clean_markers.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report the affected row count without writing",
    )
    sessions_clean_markers.add_argument(
        "--no-backup",
        action="store_true",
        default=False,
        help="Skip the timestamped state.db backup taken before writing (not recommended)",
    )

    sessions_optimize_storage = sessions_subparsers.add_parser(
        "optimize-storage",
        help="Migrate the search index to the compact v23 layout (reclaims disk on large DBs)",
        description=(
            "Rebuild the full-text search index in the compact v23 "
            "external-content layout. On large databases this reclaims a "
            "large fraction of state.db (the old layout stored duplicate "
            "copies of every message and indexed tool output). Runs "
            "foreground with a progress bar, throttles so a running gateway "
            "stays responsive, and VACUUMs at the end. Safe to interrupt and "
            "re-run — it resumes where it left off. No conversation data is "
            "changed; only the search index is rebuilt."
        ),
    )
    sessions_optimize_storage.add_argument(
        "--no-vacuum",
        action="store_true",
        default=False,
        help="Skip the final VACUUM (index is rebuilt but freed pages aren't returned to the OS until a later VACUUM)",
    )
    sessions_optimize_storage.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Skip the disk-space confirmation prompt",
    )

    sessions_repair = sessions_subparsers.add_parser(
        "repair",
        help="Repair a malformed state.db schema so hidden sessions reappear",
        description=(
            "Recover a state.db whose schema is malformed (e.g. 'table "
            "messages_fts already exists'), which makes Desktop/Dashboard show "
            "no sessions. A backup is made first; sessions and messages are "
            "preserved and the FTS search index is rebuilt if needed."
        ),
    )
    sessions_repair.add_argument(
        "--check-only",
        action="store_true",
        help="Only report whether the database opens cleanly; do not modify it",
    )
    sessions_repair.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the timestamped backup copy (not recommended)",
    )

    sessions_repair_routing = sessions_subparsers.add_parser(
        "repair-routing",
        help="Re-stamp gateway sessions that lost their routing identity",
        description=(
            "Find gateway conversations stranded in session rows whose "
            "routing identity (session_key/chat_id/origin) was never "
            "written — the damage a corrupt state.db write path leaves "
            "behind (#82616). Such a row is invisible to restart recovery, "
            "so the chat resumes an older session instead. Re-stamps each "
            "orphan from the keyed predecessor it continues, and only when "
            "that predecessor is unambiguous. Reports without touching the "
            "database unless --apply is given."
        ),
    )
    sessions_repair_routing.add_argument(
        "--apply",
        action="store_true",
        help="Perform the adoptions (default: report only)",
    )
    sessions_repair_routing.add_argument(
        "--max-gap-seconds",
        type=float,
        default=None,
        help=(
            "Window between a keyed predecessor's last activity and an "
            "orphan's start for them to count as the same conversation "
            "(default: 900)"
        ),
    )

    sessions_recover = sessions_subparsers.add_parser(
        "recover",
        help="Rebuild canonical session data into a separate clean database",
        description=(
            "Offline, non-destructive recovery for a damaged state.db. The "
            "source database and its WAL/SHM/rollback-journal sidecars are "
            "copied before SQLite opens anything. Canonical rows are rebuilt "
            "into a new output database; derived search indexes are recreated "
            "and the active database is never replaced automatically."
        ),
    )
    sessions_recover.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Source state.db or preserved backup to inspect/recover",
    )
    sessions_recover.add_argument(
        "--output",
        type=Path,
        help="New recovery database path (required unless --inspect-only)",
    )
    sessions_recover.add_argument(
        "--inspect-only",
        action="store_true",
        help="Only report canonical table readability; do not create an output database",
    )
    sessions_recover.add_argument(
        "--work-dir",
        type=Path,
        help="Existing directory for the disposable source copy (defaults beside the output)",
    )
    sessions_recover.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Rows committed per recovery batch (default: 1000)",
    )
    sessions_recover.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Best-effort salvage across damaged row ranges; the output remains "
            "separate and every skipped range is recorded"
        ),
    )
    sessions_recover.add_argument(
        "--report",
        type=Path,
        help="JSON report path (defaults to <output>.recovery.json)",
    )

    sessions_subparsers.add_parser("stats", help="Show session store statistics")

    sessions_rename = sessions_subparsers.add_parser(
        "rename", help="Set or change a session's title"
    )
    sessions_rename.add_argument("session_id", help="Session ID to rename")
    sessions_rename.add_argument("title", nargs="+", help="New title for the session")

    sessions_pin = sessions_subparsers.add_parser(
        "pin",
        help="Pin session(s) — durable keep flag, exempt from auto-archive",
        description=(
            "Set the durable 'keep' flag on one or more sessions. Pinned "
            "sessions are exempt from the sessions.auto_archive stale sweep "
            "and always appear in listings. The same flag drives the Desktop "
            "sidebar's Pinned section — pin from either surface, both see it."
        ),
    )
    sessions_pin.add_argument(
        "session_ids", nargs="+", help="Session ID(s) or unique prefix(es) to pin"
    )

    sessions_unpin = sessions_subparsers.add_parser(
        "unpin", help="Remove the pin (durable keep flag) from session(s)"
    )
    sessions_unpin.add_argument(
        "session_ids", nargs="+", help="Session ID(s) or unique prefix(es) to unpin"
    )

    sessions_pinned = sessions_subparsers.add_parser(
        "pinned", help="List pinned sessions"
    )
    sessions_pinned.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for backup/restore scripting)",
    )

    sessions_retitle = sessions_subparsers.add_parser(
        "retitle-skills",
        help="Re-title sessions whose auto-title came from a /skill's own text",
        description=(
            "Sessions opened with a /skill were auto-titled from the expanded "
            "message, which embeds the whole skill body — so the title "
            "describes the SKILL, not the request. This regenerates those "
            "titles from what the user actually typed. Lists what it would "
            "change unless --apply is passed."
        ),
    )
    sessions_retitle.add_argument(
        "--apply",
        action="store_true",
        help="Write the new titles (default: dry run)",
    )
    sessions_retitle.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum sessions to examine (default: 200)",
    )

    sessions_browse = sessions_subparsers.add_parser(
        "browse",
        help="Interactive session picker — browse, search, and resume sessions",
    )
    sessions_browse.add_argument(
        "--source", help="Filter by source (cli, discord, slack, etc.)"
    )
    sessions_browse.add_argument(
        "--limit", type=int, default=500, help="Max sessions to load (default: 500)"
    )

    sessions_import = sessions_subparsers.add_parser(
        "import",
        help="Import a Claude Code or Codex CLI session into Son of Anton",
        description=(
            "Pull a conversation started in Claude Code (~/.claude/projects) "
            "or Codex CLI (~/.codex/sessions) into the Son of Anton session store "
            "so it can be resumed with 'son-of-anton --resume <id>'. The foreign "
            "files are only read, never modified."
        ),
    )
    sessions_import.add_argument(
        "--from",
        dest="from_source",
        choices=["claude", "codex"],
        help="Which tool to import from (default: pick across both)",
    )
    sessions_import.add_argument(
        "path",
        nargs="?",
        help="Path to a specific session JSONL file (skips the picker)",
    )


    # cmd_sessions lives in son_of_anton_cli/sessions_cmd.py (main.py decomposition).
    # sessions_parser is threaded in via functools.partial because the
    # fallthrough branch calls sessions_parser.print_help() (formerly a
    # closure capture of this main()-local). The indirection through _self()
    # keeps the sessions_cmd import lazy until the subcommand actually runs
    # and lets monkeypatches on son_of_anton_cli.main.cmd_sessions keep working.
    def _dispatch_sessions(_args, *, sessions_parser=sessions_parser):
        return _self().cmd_sessions(_args, sessions_parser=sessions_parser)

    sessions_parser.set_defaults(func=_dispatch_sessions)

    # NOTE: the `son-of-anton version` subcommand was removed — `son-of-anton --version`
    # / `-V` now carries the full output including update status.

    # =========================================================================
    # Parse and execute
    # =========================================================================
    # Pre-process argv so unquoted multi-word session names after -c / -r
    # are merged into a single token before argparse sees them.
    # e.g. ``son-of-anton -c Pokemon Agent Dev`` → ``son-of-anton -c 'Pokemon Agent Dev'``
    _processed_argv = _coalesce_session_name_args(sys.argv[1:])

    # ── Defensive subparser routing (bpo-9338 workaround) ───────────
    # On some Python versions (notably <3.11), argparse fails to route
    # subcommand tokens when the parent parser has nargs='?' optional
    # arguments (--continue).  The symptom: "unrecognized arguments: model"
    # even though 'model' is a registered subcommand.
    #
    # Fix: when argv contains a token matching a known subcommand, set
    # subparsers.required=True to force deterministic routing.  If that
    # fails (e.g. 'son-of-anton -c model' where 'model' is consumed as the
    # session name for --continue), fall back to the default behaviour.
    import io as _io

    _known_cmds = (
        set(subparsers.choices.keys()) if hasattr(subparsers, "choices") else set()
    )
    _has_cmd_token = any(
        t in _known_cmds for t in _processed_argv if not t.startswith("-")
    )

    if _has_cmd_token:
        subparsers.required = True
        _saved_stderr = sys.stderr
        try:
            sys.stderr = _io.StringIO()
            args = parser.parse_args(_processed_argv)
            sys.stderr = _saved_stderr
        except SystemExit as exc:
            sys.stderr = _saved_stderr
            # Help/version flags (exit code 0) already printed output —
            # re-raise immediately to avoid a second parse_args printing
            # the same help text again (#10230).
            if exc.code == 0:
                raise
            # Subcommand name was consumed as a flag value (e.g. -c model).
            # Fall back to optional subparsers so argparse handles it normally.
            subparsers.required = False
            args = parser.parse_args(_processed_argv)
    else:
        subparsers.required = False
        args = parser.parse_args(_processed_argv)

    # Handle --version flag
    if args.version:
        cmd_version(args)
        return

    # --yolo: set SON_OF_ANTON_YOLO_MODE *before* plugin discovery.  The call to
    # _prepare_agent_startup() below triggers discover_plugins() → tool
    # imports, and tools.approval freezes _YOLO_MODE_FROZEN at module
    # import time (PR #7994, security hardening against prompt-injection).
    # If the env var is set only later (e.g. inside cmd_chat), the frozen
    # value is already False and --yolo silently does nothing.
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"

    # Discover Python plugins and register shell hooks once, before any
    # command that can fire lifecycle hooks.  Both are idempotent; gated
    # so introspection/management commands (son-of-anton hooks list, cron
    # list, gateway status, mcp add, ...) don't pay discovery cost or
    # trigger consent prompts for hooks the user is still inspecting.
    _prepare_agent_startup(args)

    # Handle top-level --oneshot / -z: single-shot mode, stdout = final
    # response only, nothing else. Bypasses cli.py entirely.
    if getattr(args, "oneshot", None):
        _confirm_startup_expensive_model_override(args)
        _run_and_exit_oneshot(
            args.oneshot,
            model=getattr(args, "model", None),
            provider=getattr(args, "provider", None),
            toolsets=getattr(args, "toolsets", None),
            skills=getattr(args, "skills", None),
            usage_file=getattr(args, "usage_file", None),
        )

    # Handle top-level --resume / --continue as shortcut to chat
    if (args.resume or args.continue_last) and args.command is None:
        args.command = "chat"
        for attr, default in [
            ("query", None),
            ("model", None),
            ("provider", None),
            ("toolsets", None),
            ("verbose", None),
            ("worktree", False),
        ]:
            if not hasattr(args, attr):
                setattr(args, attr, default)
        cmd_chat(args)
        return

    # Default to chat if no command specified
    if args.command is None:
        for attr, default in [
            ("query", None),
            ("model", None),
            ("provider", None),
            ("toolsets", None),
            ("verbose", None),
            ("resume", None),
            ("continue_last", None),
            ("worktree", False),
        ]:
            if not hasattr(args, attr):
                setattr(args, attr, default)
        cmd_chat(args)
        return

    # Execute the command.  Propagate the handler's return code as the
    # process exit code so subcommands that signal failure (e.g.
    # ``son-of-anton egress start`` refusing when credential_source=bitwarden
    # is misconfigured) actually exit non-zero.  Handlers that return
    # None are treated as success (exit 0).
    if hasattr(args, "func"):
        rc = args.func(args)
        if isinstance(rc, int) and rc != 0:
            sys.exit(rc)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
