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


def _imported_modules(tree: ast.AST):
    """Yield ``(lineno, dotted module)`` for every absolute import in ``tree``.

    Covers ``import x.y``, ``from x.y import z`` and a constant-string
    ``importlib.import_module("x.y")`` / ``import_module("x.y")`` call.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.lineno, node.module
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if name == "import_module" and isinstance(first, ast.Constant) and isinstance(first.value, str):
                if not first.value.startswith("."):
                    yield node.lineno, first.value


# Guard tests import a removed module on purpose, to prove it stays gone.
_INTENTIONALLY_MISSING = frozenset({
    ("tests/test_lsp_servers_from_path.py", "agent.lsp.install"),
    ("tests/test_no_auto_router.py", "son_of_anton_cli.router"),
})

# Imports of modules deleted by earlier removals, still sitting inert in
# try/except branches when this check was added (2026-10-15). A ratchet: the
# list may only shrink. Fix an import, then delete its entry here —
# test_known_missing_imports_are_still_missing fails until you do.
_KNOWN_MISSING = frozenset({
    ("agent/agent_runtime_helpers.py", "agent.moa_loop"),
    ("agent/agent_runtime_helpers.py", "son_of_anton_cli.moa_config"),
    ("agent/agent_runtime_helpers.py", "tools.read_preview_tool"),
    ("agent/agent_runtime_helpers.py", "tools.read_terminal_tool"),
    ("agent/agent_runtime_helpers.py", "tools.read_window_tool"),
    ("agent/agent_runtime_helpers.py", "tools.setup_mcp_tool"),
    ("agent/agent_runtime_helpers.py", "tools.tour_tool"),
    ("agent/auxiliary_client.py", "son_of_anton_cli.moa_config"),
    ("agent/chat_completion_helpers.py", "tools.browser_tool"),
    ("agent/conversation_loop.py", "agent.moa_loop"),
    ("agent/conversation_loop.py", "agent.nous_rate_guard"),
    ("agent/conversation_loop.py", "son_of_anton_cli.moa_config"),
    ("agent/model_metadata.py", "son_of_anton_cli.moa_config"),
    ("agent/tool_executor.py", "tools.read_preview_tool"),
    ("agent/tool_executor.py", "tools.read_terminal_tool"),
    ("agent/tool_executor.py", "tools.read_window_tool"),
    ("agent/tool_executor.py", "tools.setup_mcp_tool"),
    ("agent/tool_executor.py", "tools.tour_tool"),
    ("agent/verification_stop.py", "agent.verify.environment"),
    ("agent/verification_stop.py", "agent.verify.recipes"),
    ("cron/scheduler_provider.py", "plugins.cron_providers"),
    ("gateway/run.py", "tools.transcription_tools"),
    ("plugins/platforms/discord/adapter.py", "tools.transcription_tools"),
    ("plugins/platforms/discord/adapter.py", "tools.tts_tool"),
    ("plugins/platforms/discord/adapter.py", "tools.voice_mode"),
    ("plugins/platforms/discord/ffmpeg_utils.py", "tools.transcription_tools"),
    ("son_of_anton_cli/plugins.py", "agent.browser_provider"),
    ("son_of_anton_cli/plugins.py", "agent.browser_registry"),
    ("son_of_anton_cli/plugins.py", "agent.transcription_provider"),
    ("son_of_anton_cli/plugins.py", "agent.transcription_registry"),
    ("son_of_anton_cli/plugins.py", "agent.tts_provider"),
    ("son_of_anton_cli/plugins.py", "agent.tts_registry"),
    ("son_of_anton_cli/plugins.py", "son_of_anton_cli.dashboard_auth"),
    ("son_of_anton_cli/plugins.py", "son_of_anton_cli.dashboard_auth.registry"),
    ("tools/approval.py", "tools.computer_use"),
})


def _missing_module_imports() -> set[tuple[str, str]]:
    files = _first_party_files()
    modules = {_dotted(p.relative_to(ROOT)) for p in files}
    modules.discard("")
    packages = {".".join(m.split(".")[:i]) for m in modules for i in range(1, m.count(".") + 1)}
    first_party = {m.split(".")[0] for m in modules}
    missing = set()
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # reported by the compile test
        for _lineno, module in _imported_modules(tree):
            if module.split(".")[0] not in first_party:
                continue
            if module in modules or module in packages:
                continue
            missing.add((str(path.relative_to(ROOT)), module))
    return missing


def test_no_import_names_a_module_that_is_gone() -> None:
    """The symbol check above skips imports whose module no longer exists.

    That made deleting a whole module the one removal it could not see: a
    lazy ``from agent.curator import maybe_run_curator`` left inside a
    try/except would pass it silently. Any import whose top-level package is
    first-party must name a module (or package directory) that still exists.
    """
    new = sorted(_missing_module_imports() - _KNOWN_MISSING - _INTENTIONALLY_MISSING)
    assert not new, "imports of first-party modules that no longer exist:\n  " + "\n  ".join(
        f"{path}: {module}" for path, module in new
    )


def test_known_missing_imports_are_still_missing() -> None:
    """Keeps _KNOWN_MISSING honest: a fixed import must leave the list."""
    fixed = sorted(_KNOWN_MISSING - _missing_module_imports())
    assert not fixed, "no longer dangling; delete from _KNOWN_MISSING:\n  " + "\n  ".join(
        f"{path}: {module}" for path, module in fixed
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
    # Memory/skill review fork and the skill curator
    "_spawn_background_review",
    "spawn_background_review_thread",
    "cancel_background_review_for_live_turn",
    "_handle_refine_command",
    "maybe_run_curator",
    "run_curator_review",
    "apply_automatic_transitions",
    "_handle_curator_command",
    "snapshot_skills",
    "referenced_skill_names",
    "rewrite_skill_refs",
    # Model-facing skills surface (tools, prompt index, compaction ghost-skills)
    "build_skills_system_prompt",
    "clear_skills_system_prompt_cache",
    "reset_skill_view_dedup",
    "coding_compact_skill_categories",
    "compact_skill_categories",
    "SKILLS_GUIDANCE",
    "_skill_view_with_bump",
    "_resolve_skill_manage_paths",
    "_collect_protected_skill_names",
    "_collect_ghosted_skill_names",
    "_reinject_pruned_skill_markers",
    "_pruned_skill_reload_notice",
    # User-facing skill invocation (/<skill>, stacked skills, bundles,
    # /reload-skills, /learn, --skills preload, channel skills, cron skills)
    "get_skill_commands",
    "scan_skill_commands",
    "resolve_skill_command_key",
    "build_skill_invocation_message",
    "split_stacked_skill_commands",
    "build_stacked_skill_invocation_message",
    "build_preloaded_skills_prompt",
    "get_skill_bundles",
    "build_bundle_invocation_message",
    "reload_skills",
    "build_learn_prompt",
    "preprocess_skill_content",
    "register_stable_prefix",
    "find_stable_prefix",
    "extract_user_instruction_from_skill_message",
    "discord_skill_commands_by_category",
    "_strip_skill_scaffolding",
    "_ensure_skill_commands",
    "_parse_skills_argument",
    "finalize_preloaded_skills",
    "preloaded_skills",
    "_preload_skills_thread",
    "_preload_skills_result",
    "_preload_skills_error",
    "_preload_skills_requested",
    "_preload_skills_finalized",
    "_startup_skills_line_shown",
    "_available_skills_cache",
    "get_available_skills",
    "_reload_skills",
    "_pending_skills_reload_note",
    "_pending_skills_reload_notes",
    "_handle_learn_command",
    "_handle_bundles_command",
    "_handle_reload_skills_command",
    "_exec_bundles",
    "_skill_slug_from_frontmatter",
    "_check_unavailable_skill",
    "auto_skill",
    "resolve_channel_skills",
    "_resolve_channel_skills",
    "_register_skill_group",
    "_refresh_skill_catalog_state",
    "refresh_skill_group",
    "_skill_group_reserved_names",
    "_skill_group_hidden_count",
    "_skill_entries",
    "_skill_lookup",
    "skill_split_shape",
    "_preflight_check_skills",
    "_scan_cron_skill_assembled",
    "_canonical_skills",
    # Skills management, hub, sync and their dependents (removed 2026-10-15):
    # plugin skill registry, secret capture, the skills write-approval
    # subsystem, the background write origin, skill mounts/passthrough,
    # skill metrics and insights.
    "register_skill",
    "find_plugin_skill",
    "list_plugin_skills",
    "remove_plugin_skill",
    "_plugin_skills",
    "skill_namespace",
    "_portable_skill_namespace",
    "_prune_inactive_memory_provider_skills",
    "set_secret_capture_callback",
    "_secret_capture_callback",
    "_secret_state",
    "_secret_deadline",
    "_cancel_secret_capture",
    "_submit_secret_response",
    "_open_secret",
    "prompt_for_secret",
    "save_env_value_secure",
    "_handle_skills_command",
    "handle_skills_slash",
    "cmd_skills",
    "_cmd_skills_trust",
    "build_skills_parser",
    "sync_skills",
    "_sync_bundled_skills_quietly",
    "get_missing_skill_config_vars",
    "get_bundled_skills_dir",
    "get_skills_dir",
    "SKILLS",
    "skill_gist",
    "skill_pending_diff",
    "apply_skill_pending",
    "current_origin",
    "set_current_write_origin",
    "get_current_write_origin",
    "iter_skills_files",
    "register_credential_file",
    "register_credential_files",
    "clear_credential_files",
    "register_env_passthrough",
    "clear_env_passthrough",
    "record_skill_lifecycle",
    "skill_counter",
    "skill_lifecycle_fields",
    "skill_load_fields",
    "_get_skill_usage",
    "_compute_skill_breakdown",
    "_compute_skills_breakdown",
    "scan_skill",
    "scan_skill_cached",
    "should_allow_install",
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
