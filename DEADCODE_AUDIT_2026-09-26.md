# Dead code & bug audit — son-of-anton @ 8763b3a9 (2026-09-26)

Method: ruff (Pyflakes F-codes + bugbear) + AST import sweep + module-namespace
`hasattr` checks in the sealed venv (so results reflect the REAL runtime namespace,
not grep) + call-graph reachability + git archaeology on each finding.
Baseline: `scripts/run_tests.sh` = 646 passed, 0 failed — these bugs sit on paths
the suite never exercises (fresh install, model picker, /usage, update, zai,
anthropic-compat endpoints).

=====================================================================
A. LIVE / LIVE-PATH BUGS (NameErrors on real user flows)
=====================================================================

A1. `son-of-anton model` picker: api-key providers silently get no live models.
    son_of_anton_cli/models.py — five undefined names, all inside `provider_model_ids`
    (2967-3238, the function every /model picker and model check routes through —
    model_switch.py x8) or its sibling `validate_requested_model`:
      - :3022  `if normalized == "openrouter": return model_ids(force_refresh=...)`
        — `model_ids` is not defined (the real function is `provider_model_ids`;
        this looks like a rename victim). Openrouter is a LIVE provider
        (tools/openrouter_client.py + registry), so any openrouter model fetch
        raises NameError; callers that wrap in try/except fall back to the static
        catalog, the SWR background refresh at :3301 logs the error.
      - :3070  `_fetch_anthropic_models(base_url=cfg_base_url, api_key=...)` —
        self-referential fetch of a name deleted in ba850a9e (no such function in
        the module). Fires when the configured provider is `anthropic` (legacy
        config).
      - :5480  same `_fetch_anthropic_models` in `validate_requested_model`
        (5113-5713), `if normalized == "anthropic"` branch — same origin.
      - :2838/:2842  `validate_copilot_token(raw)` / `exchange_copilot_token(raw)`
        inside `_resolve_copilot_catalog_api_key` (2799-2850) — also deleted
        (copilot token-exchange helpers gone; no defs anywhere). Masked by the
        `try/except Exception: pass` at 2841-2844, so the symptom is a SILENTLY
        EMPTY pool-based Copilot catalog token → /model picker degrades to the
        stale hardcoded list for pool-only Copilot users (exactly the failure the
        docstring at 2812-2817 warns about).
    Origin: ba850a9e "remove the Anthropic Messages wire" + copilot token-flow
    excision.
    Fix: :3022 → `return provider_model_ids("openrouter", force_refresh=...)` (or
    delete the branch if openrouter fetch is handled elsewhere); :3070/:5480 →
    route anthropic fetches through the generic OpenAI-style probe or delete the
    branch; :2838/:2842 → delete the pool-exchange loop (keep the env-var path at
    2819-2827) or restore the two helpers.

A2. `son-of-anton model` picker: the generic API-key provider flow crashes.
    son_of_anton_cli/model_setup_flows.py:650 — `_prompt_api_key(...)` is called from
    `_model_flow_api_key_provider` but is never imported in that function (only the
    auth/config/models imports at 627-642). It IS defined at main.py:3046 (module
    level, so a lazy `from son_of_anton_cli.main import _prompt_api_key` would work —
    the docstring at line 11 even says the flows "import lazily inside the flows"),
    but this one import was missed. Reachable from cli_agent_setup_mixin.py:252
    (select_provider_and_model → _model_flow_api_key_provider) for any api_key
    provider (deepseek, openai-api, plugin profiles).
    NameError → broken model setup flow.

A3. Fresh-install first-run setup crashes.
    son_of_anton_cli/main.py:1417 — cmd_chat's not-configured guard (lines 1391-1421,
    the "It looks like Son of Anton isn't configured yet … Run setup now? [Y/n]" path)
    calls `cmd_setup(args)`. `cmd_setup` is NOT defined or imported anywhere in
    main.py (verified by AST over module-level scope: absent; the only setup import at
    1401 pulls is_interactive_stdin + print_noninteractive_setup_guidance).
    setup.py:1504 has `run_setup_wizard` (itself uncalled); the intended target is
    ambiguous — needs a product decision, but as written a brand-new user who answers
    "y" gets `NameError: name 'cmd_setup' is not defined`.

A4. Gateway /usage is broken on every platform.
    gateway/slash_commands.py:4145-4279 `_handle_usage_command` — dispatched live from
    gateway/run.py:15769-15770 (`if canonical == "usage"`). The function reads
    `account_lines` / `credits_lines` at lines 4242/4245, 4264/4267 and 4271-4277,
    but NEVER assigns them — the block that used to fill them (an account/credits
    fetch for the Nous billing surface) was deleted in da4b4100 along with the
    `_fetch_account_and_credits_lines` helper. Every control path through the
    function (agent-resident at 4242, history-only at 4264, and the
    no-data fallback at 4271) reaches an undefined read.
    → `NameError: name 'account_lines' is not defined` on EVERY /usage invocation
    on every gateway platform. (The gateway's slash-dispatch error handling will
    surface this as a command failure; verified there is no assignment anywhere in
    the function body by scanning lines 4145-4279.)
    Fix: delete the three dead blocks (4242-4247, 4264-4269, 4271-4278) and let
    the no-data/return paths stand — or re-wire to a real usage source if
    account/credits display is wanted again.

A5. Anthropic-compat image conversion: latent NameError on provider-gated paths.
    agent/auxiliary_client.py:3750, 3824, 7988, 8697 all call
    `_convert_openai_images_to_anthropic` (deleted in ba850a9e; the surviving helper
    is `_replace_images_with_descriptions`). All four sites are guarded by
    `_is_anthropic_compat_endpoint` (line 6925): True for provider in
    {minimax, minimax-oauth, minimax-cn} or any base URL containing "/anthropic".
    No minimax provider plugin ships in-tree (only `custom`), so today the ONLY
    trigger is a hand-configured custom endpoint whose URL contains "/anthropic" —
    e.g. the fork's own docs mention pointing custom providers at Anthropic-compatible
    relays. Such an LLM call (with images) dies with NameError mid-request,
    including the recovery-retry path (3750/3824 in _retry_same_provider_sync).
    Fix: point the four call sites at `_replace_images_with_descriptions`, or delete
    them if Anthropic-format content blocks are no longer supported at all.
    Related: `_try_anthropic` is called from `_call_llm_impl`'s provider dispatch
    (line 2068) but is also deleted — unreachable today because "anthropic" is no
    longer in PROVIDER_REGISTRY (the Messages wire is gone), but the dispatch case
    should be removed to match.

A6. `logger` NameError inside an except handler (eats the real error).
    son_of_anton_cli/cli_agent_setup_mixin.py:631 — the module never defines a
    module-level `logger` (it's only imported *inside other functions* at lines 34
    and 237). The `except Exception as exc:` branch at 630-634 calls
    `logger.warning(...)` → NameError that replaces whatever SessionResumeTooLargeError
    (or other) exception was being reported, with a confusing crash inside the
    safety-check path of session resume.

A7. Auth-store read path NameError (live; currently masked by try/except).
    son_of_anton_cli/auth.py:968 (inside `_load_auth_store`, 914-983):
    `if isinstance(raw.get("providers"), dict): _migrate_stale_nous_portal_url(raw["providers"])`
    — the function is UNDEFINED (removed in 66e16084 "remove the Nous Portal
    integration"). Reachability: the no-file branch at 916-917 RETURNS
    `{"version": …, "providers": {}}` which SKIPS the check, so this fires on
    every load of an EXISTING store whose JSON has a `providers` dict — i.e. any
    user with saved credentials. Live unprotected caller chain:
      agent/credential_pool.py:2421 → auth.py `_resolve_zai_base_url` :520 →
      `_load_auth_store()` — zai is a live provider (auth.py:1728 alias map
      glm/z-ai/z.ai/zhipu→zai; registry auto-extend includes it). So every zai
      credential-pool seeding that touches the auth store raises NameError.
    Most model_switch.py call sites (2397/2433/2462/2703/2802/2903) wrap in
    `try/except Exception: pass` — that's why the picker silently degrades to
    "no stored credentials" instead of crashing loudly.
    Fix: delete the two lines at 966-968 (the migration is a no-op now that
    Nous Portal is excised). One-line class of fix, highest blast radius.

A8. Discord skill menu fails to register: `_CMD_NAME_LIMIT` undefined.
    son_of_anton_cli/commands.py:625 (inside `_clamp_command_names`, 605-641):
    `if len(name) > _CMD_NAME_LIMIT:` — the constant is not defined anywhere in
    the module (hasattr=False; removed in 5b0a2760 "strip platforms to
    discord/signal/slack"). Call chain is LIVE: plugins/platforms/discord/adapter.py
    6279-6282 (`_refresh_skill_catalog_state`) → `discord_skill_commands_by_category`
    (commands.py:809) → `discord_skill_commands` (:779) → `_collect_gateway_skill_entries`
    (:651) → `_clamp_command_names` (:703). The NameError fires on the FIRST
    command entry processed (the comparison at :625 is unconditional — any installed
    skill or plugin command triggers it). It is caught by the
    `except Exception` at adapter.py:6267, which logs
    "Failed to register /skill command: name '_CMD_NAME_LIMIT' is not defined" —
    so the user-visible symptom is a SILENTLY MISSING /skill command + autocomplete
    on Discord (plus /reload-skills being a no-op for the catalog), not a crash.
    Check your Discord gateway's agent.log for that warning line — if it's there,
    this bug is currently active on your instance.
    Fix: define `_CMD_NAME_LIMIT = 32` at module top (the docstring at :609 states
    the 32-char platform cap the constant encodes). One line.
    (Slack does NOT use this chain — grep for `discord_skill_commands` /
    `_collect_gateway_skill_entries` in slack/adapter.py returns 0 hits — so the
    blast radius is Discord-only.)

A9. `son-of-anton tools`: three undefined names across its live flows.
    son_of_anton_cli/tools_config.py — all in functions reached by the LIVE
    `son-of-anton tools` curses UI:
      - :2012-2013 `if hidden_nous_message:` inside
        `_configure_tool_category_for_reconfig` (1996-2047), called from
        `_reconfigure_tool` (1933, :1962) — the "reconfigure existing" menu.
        Read of an undefined name (deleted with the Nous managed-billing
        surface). Fires whenever a tool category has exactly ONE visible
        provider (the `len(providers) == 1` branch at :2008) — i.e. most
        single-provider tool reconfigs (terminal, web, vision…). Uncaught
        NameError → `son-of-anton tools` "reconfigure existing" crashes mid-flow.
      - :2086 `if managed_feature:` inside `_reconfigure_provider` (2050-2105) —
        same deletion; provider that needs no env vars. Same crash.
      - :2214 `auto_configured = apply_nous_managed_defaults(...)` — directly in
        `tools_command` (2142-2462, the `son-of-anton tools` entry point), the
        first-run interactive checklist (unindented flow after the enabled-platform
        diff at :2203-2212). `apply_nous_managed_defaults` was deleted. NameError
        on the normal first interactive `son-of-anton tools` run; the
        "✓ using your Nous subscription defaults" loop at :2219-2221 and the
        `ts_key not in auto_configured` filter at :2229 are dead with it.
    Origin: da4b4100 (Nous billing/managed surface removal) left these readers.
    Fix: delete the two `if <nous>` blocks (2012-2014, 2086-2087) and replace
    :2214-2221 with `auto_configured = set()` (keeps the :2229 filter working);
    all three flows keep working with plain provider selection.

A10. Copilot runtime resolution is silently broken.
    son_of_anton_cli/runtime_provider.py:1288 and :1602 — `if provider ==
    "copilot": api_mode = _copilot_runtime_api_mode(...)`, but
    `_copilot_runtime_api_mode` is not defined anywhere (hasattr=False on the
    module; the helper was cut with the copilot token-flow excision).
    `copilot` is a LIVE provider id (auth.py:274 registry skip-list, base.py:56
    auth_type, and the xai/actual siblings at :1293-1296 show the dispatch is
    the runtime's normal path). Every copilot agent construction →
    `resolve_runtime_provider` (1324) → `_resolve_explicit_runtime` (1224,
    called at 1449) → NameError. Most agent-build paths wrap provider
    resolution in try/except and fall back, so the visible symptom is
    "copilot model silently not used / falls back to another provider" rather
    than a crash — check agent.log.
    Fix: delete both `if provider == "copilot"` branches (and restore
    `_copilot_runtime_api_mode` if copilot api-mode selection is still wanted).

A11. cli.py curses picker: dead `if False` branch with an undefined name.
    cli.py:8429-8436 `_run_curses_picker` — the thread-aware fallback was
    reduced to `if False:  # placeholder / try: _pick() / finally:
    self._status_bar_visible = was_visible` — `was_visible` is never assigned
    (NameError) and the branch is dead (`if False`). The live `else` branch
    (:8436) works, so this is latent, but it's a placeholder that should be
    resolved: either restore the real main-thread path (read `was_visible`
    before `_pick`) or delete the dead try/finally block.
    (test_tui.py:468 exercises `_run_curses_picker` via the live branch.)

A12. Hygiene-compaction provenance stamps are silently lost.
    gateway/run.py:1137 `_stamp_hygiene_compaction_provenance` — the
    function's parameter annotation references `ActivityProvenance` which is
    imported only at other call sites (17652, 17894, 24046), not in this
    function's scope. At runtime, evaluating the default/annotation for
    `provenance: "ActivityProvenance"` (string, fine) is OK, but the function
    BODY at :1142 `agent._touch_activity(desc, provenance=provenance)` is
    wrapped in `try/except Exception: logger.debug(...)` at 1143-1144 — so the
    NameError (if `ActivityProvenance` were actually referenced at runtime)
    would be swallowed. The F821 is on the ANNOTATION (string-quoted, so
    harmless at runtime) — this is a FALSE-POSITIVE-ISH annotation issue, NOT a
    live NameError. Reclassified: cosmetic; the annotation should be a string
    forward-ref or the import added locally. No user-visible bug.

A13. `son-of-anton debug share` wired to a deleted function — DEAD, not live.
    son_of_anton_cli/debug.py:676 dispatches `run_debug_share(args)`, which is
    not defined in the module. HOWEVER: `debug` is NOT in _SUBCOMMANDS (main.py:4506),
    no `build_debug_parser` exists, and no `set_defaults(func=run_debug)` binding
    anywhere — the module is unreachable from the CLI. Reclassified: the whole of
    debug.py (696 lines) is dead code (see B4); no user-visible symptom.

A14. `son-of-anton update` pre-update backup references deleted constants.
    son_of_anton_cli/update_cmd.py:2591/2608 (inside `_run_pre_update_backup`,
    called at 3210) reference `_PRE_UPDATE_SNAPSHOT_KEEP` and
    `_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE`, neither defined in the module
    (hasattr=False — verified live). main.py's lazy re-export list (line 2904)
    still names `_PRE_UPDATE_SNAPSHOT_KEEP`, so even the re-export would raise.
    Reachability: `update` is NOT a registered subcommand either (no update
    parser in _parser.py; the main.py:5143 argv branch only skips the
    interrupted-install recovery). So like debug.py, update_cmd.py (5,192 lines)
    is an ORPHANED module — latent bugs, no user-visible path. Reclassified to B4.

A15. `_run_npm_install_deterministic` (main.py:3354) references `_npm_lifecycle_env`
    at :3393 — undefined (deleted with the web-dev TUI install path). The only
    callers are update_cmd.py:2129 (dead, see A14/B4) and a docstring mention.
    So this is a latent NameError in an otherwise-dead function. Reclassified:
    part of the B4 orphaned-update surface; no live path.

=====================================================================
B. DEAD CODE
=====================================================================

B1. run_agent.py — ~1,295 duplicated lines inside class AIAgent (largest dead block).
    Method block at 4015-5309 (`check_requirements` … end of
    `_maybe_release_request_client`) is BYTE-IDENTICAL to the block at 5311-6605
    (verified: `"\n".join(lines[4014:5309]) == "\n".join(lines[5310:6605])`).
    Python keeps the LAST definition, so 4015-5309 is fully shadowed. Also duplicated:
    class constants `_VALID_API_ROLES` (4532 & 5828 — and `_VALID_API_ROLES` has
    ZERO uses anywhere in the file) and `_REQUEST_CLIENT_REUSE_REASONS` (5037 & 6333).
    Action: delete lines ~4015-5309 (the first, shadowed copy). ~2,300 lines of file
    become ~1,000.

B2. tools/web_tools.py:1425-1577 — a 150-line `if __name__ == "__main__":` demo
    block that references two undefined names (`tool_gateway_available` at 1460,
    `_get_firecrawl_gateway_url` at 1461 — both deleted in the firecrawl refactor).
    Pure scaffolding, unreachable in production, and broken. Delete the block.

B3. Untracked upstream leftovers on disk (not in git, but pollute the checkout;
    `git ls-files` shows 0 tracked files in each):
      node_modules/   181 MB  (TypeScript TUI removed 2026-08-25 per AGENTS.md)
      web/            300 KB
      website/        160 KB
      apps/           ~0 KB   (contains apps/desktop/demo, which .gitignore DOES
                               reference — but nothing is tracked)
    No tracked .py imports any of them (verified: 0 matches for
    `from (web|website|apps).` / `import web`/etc). Safe to `rm -rf` — they are
    hermes/temple upstream residue. (~182 MB reclaimed.)

B4. Orphaned command modules — whole files with zero reachable callers:
      son_of_anton_cli/update_cmd.py   5,192 lines — the `update` subcommand is NOT
          in _SUBCOMMANDS (main.py:4506: chat, completion, config, cron, gateway, mcp,
          model, pause, problem, resume, sessions, skills, status) and no update
          parser exists in _parser.py. main.py's _LAZY_COMMAND_EXPORTS re-exports
          ~70 names from it, but nothing touches those at runtime (the argv "update"
          branch at 5143 only skips _recover_from_interrupted_install). Entire file
          is dead weight — and carries the A14 latent NameErrors.
      son_of_anton_cli/debug.py           696 lines — `debug` is NOT in _SUBCOMMANDS,
          no build_debug_parser exists, no `set_defaults(func=run_debug)` binding.
          Unreachable from the CLI (confirmed: A13's "wiring" was a fiction —
          run_debug_share is deleted and nothing can dispatch to it anyway).
      son_of_anton_cli/inventory.py       854 lines — Nous-tier model helpers from
          the deleted billing surface; check_nous_free_tier / partition_nous_models_by_tier
          and most siblings have no callers.
      son_of_anton_cli/gateway.py         6,226 lines — `gateway` subcommand exists
          but only the `status` action is registered (subcommands/gateway.py adds
          only "status"). The ~6,000 lines of systemd install/stop/uninstall code
          (_SERVICE_BASE, SERVICE_DESCRIPTION, start/stop/enable paths) is dead —
          NixOS owns the service. (Those two undefined names are in exactly that
          dead region.)

B5. F401 unused imports: 113 across the tree (ruff). Cosmetic but signals the same
    decomposition drift. Top offenders: agent/conversation_loop.py, cli.py,
    son_of_anton_state.py. `ruff check --select F401 --fix` cleans most.

B6. F811 redefinitions (12):
      - run_agent.py x5 (get_activity_summary, shutdown_memory_provider,
        commit_memory_session, release_clients, close, is_interrupted) — all inside
        the B1 duplicated block; deleting B1 removes 6 of these.
      - son_of_anton_cli/main.py: `import re` redefined at 2638 and 3324 inside
        functions that already import re — harmless local shadow, but ruff flags it.
      - son_of_anton_cli/update_cmd.py: `datetime` reimport at 1086 (module already
        has `from datetime import datetime` at 33) — dead file anyway (B4).
      - plugins/platforms/slack/adapter.py: `_ssrf_redirect_guard` defined THREE
        times (57, 8590, 8673) — the last (8673) wins. Two copies are dead.
      - agent/chat_completion_helpers.py: `base_url_hostname` redefined at 516 (the
        property at 48) — the second is a real redefinition inside the property.

B7. F841 unused local variables (28) — includes a few that smell like bugs:
      - tools/process_registry.py:2178 `session` assigned but never used
      - tools/code_execution_tool.py:256 `config` assigned but never used
      (full list in ruff output)

B8. F541 f-strings without placeholders (7), all in tools/approval.py:3670-3681 —
    cosmetic.

B9. F402 import shadowed by loop variable (3), including
    tools/process_registry.py:2997 `field` — a `dataclasses.field` import gets
    clobbered by a loop variable named `field`. Check the loop isn't actually
    using the dataclass decorator later in scope (it isn't, per ruff, but the
    rename is a 1-line fix).

=====================================================================
C. ROOT CAUSE & RECOMMENDED ORDER OF ATTACK
=====================================================================

The undefined-name bugs (A1-A15) cluster around deletion/decomposition commits that
removed functionality or moved code but left callers behind:

  ba850a9e  "dead code: remove the Anthropic Messages wire"            → A1, A5, A10 (copilot helper)
  da4b4100  "remove Nous billing/subscription/credits surface"        → A4, A6 (A6's origin: nous
                                                                            entitlement format), A9 (tools_config nous gates),
                                                                            B4 (inventory.py)
  66e16084  "dead code: remove the Nous Portal integration"           → A7 (auth.py:968), A6 (A6's
                                                                            origin: nous entitlement format), A13 (A13's origin: nous
                                                                            device-auth message)
  5b0a2760  "strip platforms to discord/signal/slack: remove 28 adapters" → A8 (_CMD_NAME_LIMIT)
  main.py decomposition (god-file split)                              → A2 (model_setup_flows.py:650),
                                                                            A3 (main.py:1417),
                                                                            A6 (cli_agent_setup_mixin.py:631 logger)
  update/web-dev install-path removal                                 → A11 (cli.py was_visible), A14,
                                                                            A15 (all in dead modules — latent only)

The fix pattern is uniform: for each undefined name, either (a) restore the import
if the feature is still wanted, or (b) delete the orphaned call site if the feature
was intentionally removed. Most are (b).

Suggested priority (live, user-visible first):
  1. A7  (auth.py:968 `_migrate_stale_nous_portal_url`) — 2-line delete; un-masks the
         entire auth-store read path for any auth.json with a providers dict.
  2. A4  (slash_commands.py /usage) — delete the three dead account/credits blocks.
  3. A8  (commands.py:625) — define `_CMD_NAME_LIMIT = 32`; fixes the live Discord
         skill-menu registration.
  4. A9  (tools_config.py 2012/2086/2214) — delete the three Nous-gate blocks; fixes
         `son-of-anton tools` reconfigure + setup.
  5. A2  (model_setup_flows.py:650) — add the missing lazy import of `_prompt_api_key`.
  6. A3  (main.py:1417 cmd_setup) — wire to run_setup_wizard (or drop the prompt;
         product decision).
  7. A1  (models.py) — fix the self-referencing `model_ids` call (openrouter branch),
         the deleted `_fetch_anthropic_models` x2, and the deleted copilot token helpers.
  8. A5  (auxiliary_client.py x5) — point at `_replace_images_with_descriptions` or
         delete the call sites + the `_try_anthropic` dispatch case.
  9. A10 (runtime_provider.py x2) — delete the `if provider == "copilot"` api-mode
         branches (or restore `_copilot_runtime_api_mode`).
 10. A6  (cli_agent_setup_mixin.py:631) — module-level `logger = logging.getLogger(__name__)`.
 11. A11 (cli.py:8429-8436) — resolve the dead `if False` branch (restore or delete).
 12. B1  (run_agent.py dedup) — delete the shadowed 1,295-line block; removes 6 of
         the 12 F811s for free.
 13. B3  (rm node_modules/ web/ website/ apps/) — ~182 MB, zero risk.
 14. B4  (orphaned modules: update_cmd.py 5,192 lines, gateway.py ~6,000 dead lines,
         debug.py, inventory.py) — product decision per file; biggest footprint win
         if the fork truly ships no update/gateway-install path (NixOS owns it).
 15. A12 (gateway/run.py:1137 annotation) — cosmetic forward-ref fix.

=====================================================================
D. COMPLETE F821 TRIAGE (74 ruff hits, 27 unique names, 43 files)
=====================================================================
Legend: LIVE = reachable runtime NameError (or silently-swallowed one);
        DEAD = call site in unreachable/dead code;  ANN = string-annotation only
        (no runtime effect);  BENIGN = name exists at runtime / no impact.

LIVE (12 sites):
  gateway/slash_commands.py:4242,4245,4264,4267,4271,4273,4277 (account_lines,
    credits_lines)                                    → A4  /usage, every platform
  son_of_anton_cli/commands.py:625,626,628 (_CMD_NAME_LIMIT)
                                                       → A8  Discord skill menu
  son_of_anton_cli/tools_config.py:2012,2086,2214 (hidden_nous_message,
    managed_feature, apply_nous_managed_defaults)     → A9  `tools` reconfigure/setup
  son_of_anton_cli/auth.py:968 (_migrate_stale_nous_portal_url)
                                                       → A7  auth-store read (masked)
  son_of_anton_cli/auth.py:650,655,660 (_format_nous_entitlement_auth_error)
                                                       → A6  nous auth-error text
  son_of_anton_cli/model_setup_flows.py:650 (_prompt_api_key)
                                                       → A2  api-key model setup
  son_of_anton_cli/main.py:1417 (cmd_setup)           → A3  fresh-install prompt
  son_of_anton_cli/cli_agent_setup_mixin.py:631 (logger)
                                                       → A6  resume-safety except
  son_of_anton_cli/models.py:3022 (model_ids)         → A1  openrouter picker branch
  son_of_anton_cli/models.py:3070,5480 (_fetch_anthropic_models)
                                                       → A1  anthropic picker/validate
  son_of_anton_cli/models.py:2838,2842 (validate_copilot_token,
    exchange_copilot_token)                            → A1  copilot catalog key
  son_of_anton_cli/runtime_provider.py:1288,1602 (_copilot_runtime_api_mode)
                                                       → A10 copilot agent build
  agent/auxiliary_client.py:3750,3824,7988,8697 (_convert_openai_images_to_anthropic)
                                                       → A5  /anthropic custom URLs
  son_of_anton_state.py:3773 (fts5_cjk_so_path)       → latent (CJK self-heal path)

DEAD (call sites in unreachable code — latent NameErrors):
  agent/auxiliary_client.py:2068 (_try_anthropic)     → A5 (anthropic registry gone)
  son_of_anton_cli/update_cmd.py:757,2591,2608 (shutil,
    _PRE_UPDATE_SNAPSHOT_KEEP, _PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE)
                                                       → A14 (update_cmd orphaned)
  son_of_anton_cli/debug.py:676 (run_debug_share)     → A13 (debug orphaned)
  son_of_anton_cli/gateway.py:1698,1699,2838,2880 (_SERVICE_BASE,
    SERVICE_DESCRIPTION)                               → B4 (systemd install dead)
  son_of_anton_cli/main.py:3393 (_npm_lifecycle_env)  → A15 (update-only caller)
  son_of_anton_cli/inventory.py:839,844 (check_nous_free_tier,
    partition_nous_models_by_tier)                     → B4 (nous tier dead)
  son_of_anton_cli/auth.py:4479 (_nous_device_auth_timeout_message)
                                                       → A13-origin (nous device
                                                          auth removed; _poll_for_token
                                                          is the nous loopback path)
  cli.py:8433 (was_visible)                            → A11 (dead `if False` branch)
  tools/web_tools.py:1460,1461 (tool_gateway_available,
    _get_firecrawl_gateway_url)                        → B2 (__main__ demo block)

ANN (string annotations only — no runtime effect):
  agent/agent_init.py:973 ("RateLimitState")
  agent/auxiliary_client.py:2068,3750,3824,7988,8697 (some dual LIVE/ANN)
  agent/conversation_loop.py:2651 (ActivityProvenance)
  agent/codex_runtime.py:448 (ToolExecutor — importable alias)
  cli.py:1660,3612,3642,3651,3778,4185,4366,4370,4473,7207,7218,7469,7475,7481,7488,7509,
    7515,7522,7544,7660,7681,7701,7722,7743,7764,7864 (various TYPE aliases)
  gateway/run.py:1137 (ActivityProvenance)             → A12
  physics_intern/llm.py:448 (ToolExecutor)
  plugins/memory/holographic/retrieval.py:460 (np.ndarray)
  son_of_anton_cli/gateway.py:1702,2822 (Path)
  son_of_anton_state.py:3757,3758,3778 (_FTS_CJK_TRIGGERS, FTS_CJK_STALE_KEY —
    both DO exist at module level; ruff false positive from the import form)
  tools/async_delegation.py:350 (Any)
  tools/mcp_tool.py:1130 (Any)
  tools/process_registry.py:1297 (Any)
  tools/send_message_tool.py:1125 (_HOME_CHANNEL_ENV_OVERRIDES — dict constant,
    ruff false positive: defined at module level in that file)
  tools/web_tools.py:1460,1461 (dual DEAD/ANN)

Notes:
- `_FTS_CJK_TRIGGERS` / `FTS_CJK_STALE_KEY` / `_HOME_CHANNEL_ENV_OVERRIDES` are
  DEFINED in their modules; ruff flags them because of how the import is written
  (e.g. `from son_of_anton_state_common import (...)` inside a try/except) —
  treat as ruff false positives, not bugs.
- `web_tools.py:1460 os` was a ruff false positive (os IS imported); the real
  undefined names there are tool_gateway_available / _get_firecrawl_gateway_url (DEAD).
- The 24 CLI-annotation hits in cli.py are all TYPE-alias strings in
  `Optional[...]` / `dict[...]` annotations — zero runtime effect; safe to fix
  later with `from __future__ import annotations` or local imports if desired.

Files this audit did NOT fully trace (time-boxed):
  - physics_intern/ (615 lines, small, no flags beyond one annotation F821)
  - gateway/platforms/signal.py (built-in, no F821)
  - plugins/memory/* (holographic retrieval.py has 1 annotation-only F821)
  - skills/ bundled scripts (out of scope for core audit)

=====================================================================
E. REMEDIATION APPLIED (2026-09-26, this tree)
=====================================================================
All A-findings fixed, all B/C dead code removed, verified:
  scripts/run_tests.sh = 646 passed, 0 failed (3 runs, incl. post-hangup).
  ruff F821: 74 → 0. ruff F601: 3 → 0. Every touched file compiles.

Fixes (each = 1–3 lines, except the dead-code cuts below):
  - auth.py: deleted the `_migrate_stale_nous_portal_url()` call in
    `_load_auth_store` (A7); deleted the orphaned generic device-code
    poller pair `_request_device_code`/`_poll_for_token` + the
    `DEVICE_AUTH_POLL_INTERVAL_CAP_SECONDS` constant (dead since 66e16084);
    `format_auth_error` no longer calls the deleted
    `_format_nous_entitlement_auth_error` (A15).
  - gateway/slash_commands.py: `/usage` — removed the three blocks reading
    the never-assigned `account_lines`/`credits_lines` (A4).
  - commands.py: restored `_CMD_NAME_LIMIT = 32` (value recovered via
    `git log -S`; the Discord /skill menu registration had been silently
    failing) (A8).
  - tools_config.py: removed the dangling `hidden_nous_message` block,
    `managed_feature` reference, and the `apply_nous_managed_defaults()`
    call (replaced with `auto_configured = set()`, preserving the filter
    semantics — nothing is auto-configured anymore) (A13).
  - main.py first-run: `cmd_setup` (undefined; the `setup` subcommand was
    cut in bebf5cd6) → lazy `run_setup_wizard` import; the "Run setup now?"
    prompt no longer offers a non-existent `son-of-anton setup` command (A5).
  - models.py (A1): openrouter picker branch now calls the surviving
    `fetch_api_models()` live catalog; both `_fetch_anthropic_models`
    sites (picker + `validate_requested_model`) fall back to the curated
    catalog, matching ba850a9e's stated intent (no Anthropic wire); the
    copilot pool-token path uses the pool token as-is — the exchange/
    validation helpers died with the ACP client (a9563978), and the
    pool entry already holds a valid token.
  - auxiliary_client.py (A11): restored `_convert_openai_images_to_anthropic`
    (self-contained data transform, was serving LIVE minimax/compat
    endpoints via `_is_anthropic_compat_endpoint`); deleted the three
    vestigial `_try_anthropic()` branches — `anthropic` is not in
    PROVIDER_REGISTRY and `agent/anthropic_adapter.py` no longer exists;
    added `_try_anthropic` to tests/test_no_orphan_references.py's
    removed-symbol denylist.
  - runtime_provider.py: both copilot `_copilot_runtime_api_mode` sites →
    `api_mode = "chat_completions"` (the deleted helper's default) (A9).
  - model_setup_flows.py: added the dropped
    `from son_of_anton_cli.main import _prompt_api_key` (A10).
  - cli_agent_setup_mixin.py: added the missing lazy
    `from cli import logger` in the except handler at :631 (A3).
  - cli.py: removed the two dead `if False:` blocks that referenced the
    undefined `was_visible` (A14) and the live `_pick()` (the
    `else: return None` fallback was unreachable).
  - cron/executions.py: added the missing `from pathlib import Path` (A2).
  - son_of_anton_state.py: fixed the CJK self-heal branch referencing the
    deleted `fts5_cjk_so_path()` — it now warns without the non-existent
    path (the `_ensure_fts_cjk_schema` method has no callers today, but
    the module must not NameError if it ever gets one) (A6).
  - tools/send_message_tool.py: removed the dangling
    `_HOME_CHANNEL_ENV_OVERRIDES` reference (deleted in 5b0a2760) — the
    error message now states the fact directly (A16).
  - tools/web_tools.py: removed the dangling `tool_gateway_available` /
    `_get_firecrawl_gateway_url` branch in the `__main__` demo block (A12).
  - Annotation-only F821s (no runtime effect) fixed properly: TYPE_CHECKING
    imports added in agent/agent_init.py, gateway/run.py,
    tools/patch_parser.py, plugins/context_engine/__init__.py,
    plugins/web/firecrawl/provider.py; physics_intern/llm.py
    `ToolExecutor` annotation → `Any`; plugins/memory/holographic/
    retrieval.py `np.ndarray` under TYPE_CHECKING.
  - Left as documented false positive: son_of_anton_tui/__init__.py:7
    `TextualBackend` in `__all__` (resolved via the module's PEP 562
    `__getattr__` lazy import).

Dead code removed (2,377 deletions, 194 insertions, 29 files):
  - son_of_anton_cli/update_cmd.py — DELETED (5,192 lines; the `update`
    subcommand was never registered in _parser.py/_SUBCOMMANDS, zero
    callers, zero test refs). Also removed: main.py's
    `_LAZY_COMMAND_EXPORTS["son_of_anton_cli.update_cmd"]` table entry
    (~70 names), the stale "Update pipeline lives in update_cmd.py"
    comment, and the 3-function npm/nixos dead chain in main.py
    (`_nixos_build_env`, `_run_npm_install_deterministic`,
    `_run_npm_watching_for_engine_failure` — only caller was update_cmd).
    NOTE: the main.py cut initially over-deleted one line and clipped
    `def _load_installable_optional_extras` (a LIVE function called by
    _install_repair.py:159); caught via the resulting F821 on its body's
    `group` param and restored before the first green test run.
  - run_agent.py — removed the 1,296-line shadowed duplicate block
    (4015–5310, byte-identical to 5311–6605; 48 methods redefined).
    Deleting the first copy also resolved 6 of the F811 redefinitions.
  - son_of_anton_cli/debug.py — carved 696 → ~180 lines: kept only the
    paste-lifecycle half (the gateway's cron ticker imports
    `_sweep_expired_pastes`); removed the dead `debug share`/`delete`
    report-collection + upload + redact machinery (zero external refs, no
    `debug` subcommand exists; the module also referenced the never-defined
    `run_debug_share`).
  - inventory.py + model_switch.py — removed the whole dead
    `force_fresh_nous_tier` threading (param on 3 signatures, docstring,
    2 call-site pass-throughs, the `check_nous_free_tier` /
    `partition_nous_models_by_tier` call block, the `nous_free_tier`
    local) — no caller ever passed it.
  - F601 duplicate dict keys removed: model_normalize.py
    (`"trinity"` x2, identical value), model_switch.py (`"api_mode"` x2),
    models.py (`"anthropic/claude-sonnet-5"` x2 — the bare
    `"claude-sonnet-5": "claude-sonnet-5"` mapping stays).

Deliberately NOT done (scoped out, see report §C priorities):
  - The 111 F401 unused-import hits: all pre-exist at HEAD; bulk
    `ruff --fix` would break the PEP 562 lazy-reexport patterns in
    main.py and son_of_anton_tui/__init__.py. Pre-existing lint debt,
    not part of this audit's bug/dead-code findings.
  - gateway.py service-lifecycle branches (`install`/`uninstall`/`start`/
    `stop`/`restart`/`migrate-legacy` in `_gateway_command_inner` are
    unreachable — the parser only routes `run`/`status`, and the service
    functions have zero external callers; BUT `ensure_gateway_service`
    and `install_linux_gateway_from_setup` are live via setup.py /
    backup.py, and `get_service_name`/`get_systemd_unit_path` have ~30
    live refs, so the carve must be surgical). Needs its own pass with
    the systemd test matrix.
  - Untracked build leftovers web/ website/ apps/ node_modules/ — not
    touched (not tracked, not imported; deletion is a local-hygiene
    decision, not a code change).
