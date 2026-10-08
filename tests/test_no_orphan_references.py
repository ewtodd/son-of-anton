"""Every module compiles, and every `from X import name` names something real.

The gap these close was found the hard way. A syntax error shipped in
agent/transports/chat_completions.py and survived both the test suite and
`nix flake check`, because the venv sweep imports a hand-written list of ~20
modules and that file is imported lazily inside a try/except elsewhere. And a
`from gateway.run import _profile_runtime_scope` outlived the profiles
removal by two sessions, inert inside a try/except, invisible to the compiler
and to every import sweep.

Both are the same class: a reference to something that no longer exists,
hidden from the interpreter because nothing on the tested path evaluates it.
Aggressive deletion is only safe with these two checks standing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".venv", "result", "node_modules", ".mypy_cache"}


def _first_party_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # Skill scripts run as standalone subprocesses against their own deps.
        if rel.parts and rel.parts[0] in {"skills", "optional-skills"}:
            continue
        out.append(p)
    return sorted(out)


def _dotted(rel: Path) -> str:
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def test_every_module_compiles() -> None:
    """A syntax error anywhere is a shipped crash, however lazily imported."""
    broken = []
    for p in _first_party_files():
        try:
            ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        except SyntaxError as exc:
            broken.append(f"{p.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
    assert not broken, "modules with syntax errors:\n  " + "\n  ".join(broken)


def _module_exports(path: Path) -> set[str] | None:
    """Top-level names a module binds, including inside if/try blocks.

    Returns None when the module defines ``__getattr__`` (PEP 562): it can
    synthesise any name, so its exports cannot be known statically.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    names: set[str] = set()

    def collect(nodes) -> None:
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                names.add(n.target.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.If):
                collect(n.body)
                collect(n.orelse)
            elif isinstance(n, ast.Try):
                collect(n.body)
                collect(n.orelse)
                collect(n.finalbody)
                for h in n.handlers:
                    collect(h.body)

    collect(tree.body)
    if "__getattr__" in names:
        return None
    return names


def test_no_import_names_a_symbol_that_is_gone() -> None:
    """Catches a removal that left its callers behind.

    Scoped to first-party modules — third-party packages are the installer's
    problem, and their conditional exports would produce noise.
    """
    files = _first_party_files()
    modules = {_dotted(p.relative_to(ROOT)): p for p in files}
    modules.pop("", None)
    exports: dict[str, set[str] | None] = {}
    orphans = []

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # reported by the compile test
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if node.module not in modules:
                continue
            if node.module not in exports:
                exports[node.module] = _module_exports(modules[node.module])
            have = exports[node.module]
            if have is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in have:
                    continue
                if f"{node.module}.{alias.name}" in modules:
                    continue  # importing a submodule, not a symbol
                orphans.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: "
                    f"from {node.module} import {alias.name}"
                )

    assert not orphans, (
        "imports naming symbols that no longer exist:\n  " + "\n  ".join(orphans)
    )


# Symbols deleted when their subsystem was removed. The import test above
# cannot see these: they were reached as ``self._foo(...)`` /
# ``agent._foo(...)``, which no static import check inspects. Four of them
# survived their own removal that way — a call into a method that no longer
# existed, inert only because the caller sat in a branch that could no longer
# be taken. An explicit denylist is cheap and catches exactly that.
_REMOVED_SYMBOLS = (
    # Anthropic Messages wire
    "_anthropic_messages_create",
    "_rebuild_anthropic_client",
    "_anthropic_preserve_dots",
    "_prepare_anthropic_messages_for_api",
    "_create_request_anthropic_client",
    "_close_request_anthropic_client",
    "_abort_request_anthropic_client",
    "_close_cached_request_anthropic_client",
    "_try_refresh_anthropic_client_credentials",
    "_sync_anthropic_entry_from_credentials_file",
    "_try_anthropic",
    # Nous Portal
    "_sync_nous_entry_from_auth_store",
    "_nous_invoke_jwt_is_usable",
    "resolve_nous_runtime_credentials",
    "_agent_key_is_usable",
    "nous_api_mode",
    # Copilot
    "_is_copilot_provider",
    "_is_copilot_url",
    "_copilot_headers_for_request",
    # Qwen Portal
    "_is_qwen_portal",
    "_qwen_portal_headers",
    # Codex app-server
    "_run_codex_app_server_turn",
    "_maybe_apply_codex_app_server_runtime",
    # Codex Responses / OpenAI OAuth (removed with the Responses transport)
    "host_mandated_api_mode",
    "copilot_model_api_mode",
    "_should_use_copilot_responses_api",
    "resolve_codex_runtime_credentials",
    "refresh_codex_oauth_pure",
    "_login_openai_codex",
    "_codex_device_code_login",
    "_save_codex_tokens",
    "_read_codex_tokens",
    "_recover_codex_tokens_from_cli",
    "_refresh_codex_auth_tokens",
    "_import_codex_cli_tokens",
    "get_codex_auth_status",
    "_codex_access_token_is_expiring",
    "_codex_pool_rate_limit_status",
    "_codex_usage_probe_url",
    "_is_codex_rate_limit_shaped",
    "_is_terminal_codex_oauth_refresh_error",
    "_pool_codex_access_token",
    "_probe_codex_quota_restored",
    "_codex_quota_restored_upstream",
    "_sync_codex_entry_from_auth_store",
    "_sync_codex_pool_entries",
    "clear_codex_pool_quota_cooldowns",
    "_verified_codex_ctx_for_slug",
    "_codex_oauth_token_fingerprint",
    "_extract_chatgpt_account_id",
    "_fetch_codex_oauth_context_lengths",
    "_fetch_codex_oauth_context_lengths_with_source",
    "_resolve_codex_oauth_context_length",
    "_resolve_codex_oauth_context_length_with_source",
    "_should_sanitize_tool_calls",
    "_client_streams_internally",
    "_fire_streamed_codex_commentary",
    "_prune_stale_reasoning_replay",
    "_is_codex_interim",
    "is_rate_limited_auth_error",
    # Vendor OAuth flows / credential-pool refresh (removed 2026-10-08)
    "resolve_spotify_runtime_credentials",
    "login_spotify_command",
    "get_spotify_auth_status",
    "refresh_xai_oauth_pure",
    "resolve_xai_oauth_runtime_credentials",
    "get_xai_oauth_auth_status",
    "_sync_xai_oauth_entry_from_auth_store",
    "_sync_xai_oauth_entry_from_pool_store",
    "_sync_device_code_entry_to_auth_store",
    "_refresh_entry",
    "_refresh_entry_impl",
    "_entry_needs_refresh",
    "_refresh_pending_entries",
    "_single_use_refresh_lock_timeout",
    "try_refresh_current",
    "try_refresh_matching",
    "_write_through_provider_state_to_global_root",
    "_seed_from_singletons",
    "_resolve_kimi_base_url",
    "_resolve_zai_base_url",
    "detect_zai_endpoint",
    "_probe_single_zai_endpoint",
    "resolve_external_process_provider_credentials",
    "get_external_process_provider_status",
    "get_minimax_oauth_auth_status",
    "_login_minimax_oauth",
    # Auth-store helpers removed with the OAuth surface
    "_save_provider_state",
    "_load_provider_state",
    "_load_provider_state_with_source",
    "_store_provider_state",
    "_persist_provider_state_to_store",
    "get_provider_auth_state",
    "is_actual_local_base_url",
    "normalize_actual_base_url",
    "_normalize_lmstudio_runtime_base_url",
    # Profiles / Kanban
    "_profile_runtime_scope",
    "_multiplex_profile_homes",
    "scrub_kanban_env",
    "is_dispatcher_owned_worker_context",
    "build_kanban_stop_nudge",
)


def test_no_reference_to_a_removed_symbol() -> None:
    """Tokenized, so a comment or docstring naming the old thing is fine.

    Only NAME tokens count — a reference the interpreter would actually
    resolve. Prose that explains what used to be there is documentation, not
    a dangling call.
    """
    import io
    import tokenize

    removed = set(_REMOVED_SYMBOLS)
    hits = []
    for path in _first_party_files():
        if path.name == "test_no_orphan_references.py":
            continue  # this file names them all on purpose
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tokens = tokenize.generate_tokens(io.StringIO(src).readline)
            for tok in tokens:
                if tok.type == tokenize.NAME and tok.string in removed:
                    hits.append(
                        f"{path.relative_to(ROOT)}:{tok.start[0]}: {tok.string}"
                    )
        except (tokenize.TokenError, IndentationError, SyntaxError):
            continue  # reported by the compile test
    assert not hits, "references to removed symbols:\n  " + "\n  ".join(hits)
