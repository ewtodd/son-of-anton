# Son of Anton — Status & Handoff

Written 2026-08-22 after the fork/build-out session. Last updated **2026-09-03**.
State as of commit `c4635dac` on `main` (`github.com:ewtodd/son-of-anton`),
plus two uncommitted rounds (see "Textual front-end — bug round" and
"Nine-agent research pipeline removed").

Two things below reverse earlier entries; the dated sections are kept as
history, but read the later one:

- **The TUI is back, and it is now the only interface.** The 2026-08-25
  "TUI removed" round killed a *different* TUI (the `ui-tui/` TypeScript
  app). A Textual front-end replaced it, and on 2026-09-02 `9906a30b` deleted
  the prompt_toolkit REPL, so there is no longer a CLI to fall back to and
  no `--tui` flag to pass.
- **Profiles are gone.** `2067d1be` + `292ab53e` deleted the profiles system
  and gateway multiplexing (8k lines); `7e79f545` replaced them with one
  systemd service per account.

---

## What this is

`son-of-anton` is a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (upstream commit `fcbd1076a9`), stripped to a lean always-on daemon
surface, extended with the physics mode from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`). MIT. It is the successor to the archived
[temple](https://github.com/ewtodd/temple) harness — the daemon design,
permission modes, and request router carry over from it.

Two agent modes. A keyword router classifies the FIRST message of a session
(physics gets no conversation history, so re-routing a follow-up would
discard the exchange); `/mode` pins one explicitly on any turn, and
`router.modes` switches physics off per deployment:
- `standard` — the hermes agent loop (terminal, files, web, skills, memory, delegation, cron)
- `physics` — Autophysicist: single research manager, permanent memory + scratchpad, token budget, `submit_final_answer`, git workspace, per-iteration critic

The nine-agent research pipeline that physics-intern shipped was ported and
then removed as redundant — see "Nine-agent research pipeline removed".

---

## What's done

### Phase 1 — temple archived
- README carries the ARCHIVED statement (successor link to ewtodd/son-of-anton)
- Local commit `f8e6559` / user re-edited and archived the GitHub repo themselves
- My follow-up link-fix commit was dropped when the user re-archived

### Phase 2 — fork + rename (hermes → renco → son-of-anton)
- New history: commits authored by `son-of-anton-bot`, co-author `Ethan Todd`
- Full mechanical rename: binary `son-of-anton`, package `son_of_anton_cli`/`son_of_anton_*`, env `SON_OF_ANTON_*`, home `~/.son-of-anton`, npm scope `@sonofanton/*`
- History rewritten (filter-branch) twice: dropped the DeepSeek co-author trailer, then renamed renco→son-of-anton

### Phase 3 — bloat strip
- Removed: Electron desktop app, web dashboard + `web_server.py`, docs website, ACP adapter, docker, native CJK FTS, i18n locales, optional-skills/mcps, research/trajectory tooling, ~25 plugins, 37→3 model providers (custom/openai/deepseek), browser/TTS/voice/wake/video/image-gen/HA/kanban/computer-use/x_search/feishu/yuanbao tools, MoA core, pets, CI, ~half the tests
- Kept: CLI, TUI, gateway core, cron, curator, memory providers, MCP client, iron proxy, discord tool, terminal backends local+ssh
- Core tools: web_search/extract, terminal, process, file ops, vision_analyze, skills, todo, memory, session_search, clarify, execute_code, delegate_task, cronjob

### Phase 4 — platforms + router + permissions
- Gateway down to Discord (`plugins/platforms/discord/`), Slack (`plugins/platforms/slack/`), Signal (`gateway/platforms/signal.py`)
- `son_of_anton_cli/router.py`: `classify_mode()` (physics/research/standard keywords), config section `router:`
  (the ported `classify_complexity()` model-slot half was never wired to model resolution and was deleted 2026-09-02)
- `/mode auto|standard|physics|research`, `/model auto|NAME`, `/perm default|ask|lockdown|yolo`
- `:q`/`q` quit (was aliased to `queue` in hermes — fixed); command resolution accepts `:` prefix

### Phase 5 — Nix packaging + per-user daemons
- `nix build .#default` and `nix flake check` fully green
- Python-only package: sealed uv2nix venv + wrapper exposing bundled skills/plugins (SON_OF_ANTON_BUNDLED_SKILLS/_PLUGINS)
- Home Manager module = per-account daemon (one gateway per work/play account, own `~/.son-of-anton`; set `users.users.<name>.linger = true`)
- NixOS module (system service, dedicated user) also available
- pyproject extras pruned; uv.lock regenerated
- venv-imports check imports the full surface (98 modules incl. both physics modes)

### Phase 6 — physics modes
- `physics_intern/` vendored at repo root
- `physics_intern/llm.py`: OpenAI-compatible layer resolving endpoints from config.yaml (`physics.base_url` → deepseek/openai defaults → `custom_providers` → `127.0.0.1:8080/v1`), retry + context-length detection
- Autophysicist callable as `run_autophysicist(...)`
- Experimental verification: numeric `checks`, workspace `RESULTS.txt`, optional checker script, `FORMAL_EVAL.md`
- Toy problems in `problems/` (data synthesized by the model — real UM-ANSG data is private)
- Wired into CLI chat() and gateway turns; wheel ships physics data via package-data

### Theming (kitty)
- Banner ASCII art plain text; default skin colors ANSI names; `_hex_to_ansi` snaps custom-skin hex to the legacy-16 palette; rich Consoles use `color_system="standard"`; diff/streamed text palette-based; caduceus ⚕ replaced everywhere with the ⚛ atom

### Version
- v0.2.0 (temple was 0.1.0), release date 2026.8.22

### Phase 7 — this session (2026-08-22, `1a338e19` → `fc5e9eba`)

1. **Pet subsystem removed** (`1a338e19`) — the base-CLI pet pane (~280 lines:
   sprite window, anim thread, config poller), its init state, the
   prompt_toolkit widget, and the dead `/pet` + `/hatch` dispatch branches
   (neither command existed in the registry). `_on_reaction` kept as a no-op
   hook so the reaction core's interactive-host wiring stays intact.

2. **Provider catalog pruned to the fork's surface** (`84c7c5e1`) —
   CANONICAL_PROVIDERS (38→2) and PROVIDER_REGISTRY (37→2) now carry only
   deepseek + openai-api, plus config.yaml custom endpoints (the `custom`
   plugin profile). Pruned: provider overlays/aliases/labels in
   `providers.py`, the 18→3 model-setup flows in `model_setup_flows.py`,
   the Nous Portal one-shot (`setup --portal`), the TTS setup section, Nous
   subscription tool-status rows, quick-setup's Nous flow, the
   openrouter/nous curated-model branches in `model_switch.py`, and the
   status/tools/doctor displays of removed providers. models.dev stays as
   the generic runtime catalog.

3. **.env.example + cli-config.yaml.example pruned** (`7594fe91`) —
   .env.example 504→146 lines (removed 40+ removed-provider keys, browser/
   voice/modal keys, removed platforms). OPTIONAL_ENV_VARS (the setup
   wizard's env prompt list) 91→34 entries. cli-config.yaml.example
   regenerated compactly with verified fork sections. DEFAULT_CONFIG
   openrouter copy rewritten; inert `openrouter` config section removed.

4. **AGENTS.md rewritten for the fork** (`a06e4108`) — dropped the
   hermes-era desktop/dashboard/kanban/pets/CI/i18n content; documents the
   three-mode router, the provider surface, physics modes, per-user daemons,
   and the pruned plugin/memory/model-provider sets. Verified against the
   tree (43 bundled skills, 92 slash commands, 98-module sweep).

5. **Test suite replaced + pre-commit wired** (`a8f3a886`) — the 2262-file
   hermes suite (mostly removed-feature tests) is gone; in its place a
   9-file, 52-test contract suite: router classification, provider-catalog
   invariants, physics-mode imports/endpoint resolution/formal evaluation,
   config deep-merge, profile home isolation, slash-command registry
   invariants, core-tool registration, skin fallback. `.pre-commit-config.yaml`
   wires ruff (PLW1514) + the suite (<1s); `pre-commit` added to the dev
   shell. Also dropped the two desktop_ui tool files whose imports were
   broken by the desktop removal, and fixed 14 pre-existing PLW1514
   violations so the ruff gate is clean.

6. **TUI quit parity** (`fc5e9eba`) — the TUI still had hermes' q→queue
   aliasing and ignored colon-prefixed commands (`:q` went to the agent as
   chat). Now `/q`, `:q`, `/exit` quit and `/queue` queues, matching the CLI.
   Vitest 1703 pass + tsc clean.

### Deep-Nous cleanup layers (this session, `40a24437` + `da4b4100`)

- **Layer 1** (`40a24437`): gateway enroll (relay connector), debug share
  --nous, nous_auth_keepalive, `son-of-anton sync` org skills-sync +
  skills_sync_client + org-mirror resolution in skill_utils/prompt_builder/
  skills_tool + org write-guards/auto-propose in skill_manager_tool + sync
  opt-in flags, OPENROUTER_MODELS/fetch_openrouter_models/openrouter slug
  detection/fetch_ollama_cloud_models + scripts/build_model_catalog.py, the
  cron.chronos config section. `_model_flow_api_key_provider` rewritten
  generic (also fixed a latent NameError on `_select_zai_endpoint`).
  Bundled-skills sync (`tools/skills_sync.py`) is local and stays.
- **Layer 2** (`da4b4100`): /subscription + /topup (CLI, gateway, TUI RPCs),
  cli_billing_mixin, billing/subscription/usage views, nous_billing,
  credits_tracker + account_usage and every call site (run_agent,
  conversation_loop, agent_init, tui_gateway, cli_agent_setup_mixin),
  nous_subscription + the managed tool gateway (tools_config rows, firecrawl
  gateway path, tool_backend_helpers helpers, system-prompt subscription
  block), and the Nous 401/entitlement retry paths. /usage keeps its session
  token/rate-limit display; /insights stays (local analytics).

### Live deployment round (2026-08-23, `ebf39135` → `bcf72b1e`)

The gateway went live on e-desktop (NixOS system service) serving Signal via
the mu signal-cli HTTP daemon, routing through litellm on oracle →
llama-swap on son-of-anton. Every turn-class bug below was found by the live
bot, not by the suite — compile and import sweeps can't see function-body
NameErrors or sealed-venv import gaps.

- **Physics smoke fixes** (`012bd9df`): Config fallback for unregistered
  models (models.yaml dropped), `build_config(overrides=)` kwarg, compute
  sandbox cwd (RESULTS.txt at the workspace root), and the
  `verification.core` import in `render_formal_evaluation`.
- **Gateway router config** (`aa8bf7e3`): `_resolve_session_agent_mode`
  called `.get()` on a GatewayConfig object — AttributeError on every turn.
  Now reads `router:` through the canonical config loader.
- **dict-form custom_providers** (`aa8bf7e3` + `ebf39135`): the runtime
  rejected the keyed-dict form the fork's own examples emit; normalized in
  `get_compatible_custom_providers` and accepted in config validation.
- **i18n restored** (`aa8bf7e3`): the locale prune deleted the catalogs but
  left `agent/i18n` + call sites, so gateway strings rendered raw dotted
  keys. `locales/en.yaml` restored from upstream at the fork commit,
  rebranded, shipped via `SON_OF_ANTON_BUNDLED_LOCALES`.
- **Sealed-venv plugins import** (`0cadb4fd`): the runtime wrapper never put
  the bundled share dir on PYTHONPATH, so top-level `import plugins` failed
  in the sealed venv (web tools, memory, cron providers). Wrapper now
  prefixes PYTHONPATH; the venv-imports check imports the web providers
  directly so the gap can't regress.
- **Credits-removal stragglers** (`0cadb4fd` + `ebf39135`): two orphaned
  calls into the deleted credits subsystem — `agent._credits_latch =
  new_credits_latch()` in init_agent (agent build NameError) and
  `agent._capture_credits(response)` in the streaming path (first-LLM-call
  AttributeError). An agent-build regression test now guards this class.
- **Approval guard UnboundLocalError** (`27204186`): `approval_mode` was
  only assigned under lockdown, so every terminal call crashed in
  `check_all_command_guards`. Initialized from `approvals.mode`.
- **`/profile <name>` per-chat switch** (`e8ce246d`): a multiplexed gateway
  can pin a chat to a profile with a command (persisted in
  `profile_pins.json`), so one Signal number serves both accounts without
  group routes. Resolution: explicit routes > chat pin > default.
- **Shared-account adapter dedupe** (`571a88e7`): all three profiles
  connected to the same Signal daemon, so every inbound message got three
  replies. `_adapter_credential_fingerprint` now covers URL+account
  platforms; the first profile owns the adapter and the rest are skipped.
  Bare `/profile` reports the chat's profile instead of listing all.
- **EACCES-hardened context scans** (`bcf72b1e`): with cwd = the user's
  0700 home, `_find_git_root` raised PermissionError mid system-prompt
  build. Git-root discovery, the AGENTS.md chain, and .cursorrules now
  treat unreadable dirs as "not found".
- **Emoji strip** (`bcf72b1e`): ~340 decorative glyphs removed from
  user-facing gateway chat replies (i18n catalog + command/reply strings).
  Logs and the CLI spinner keep theirs.
- **Profile HOME** (`0f3ee9f0`): terminal subprocesses kept the service
  user's HOME, so `~` in a profile pointed at `/var/lib/son-of-anton` — the
  model anchored to the wrong README exactly because of this. New
  `terminal.home_mode = "cwd"` makes subprocess HOME resolve to the
  command's working directory (each profile's cwd is its user's home).

---

### UI hygiene round (2026-08-23, this session — interactive CLI/TUI report)

The user ran an interactive session and filed five problems: the session's
cwd didn't match the spawn directory, qwen served garbage (model-side, not
the repo), the agent still self-identified with Nous Research, emoji
replacement was broken (raw ANSI leak on the update notice + glyphs making
it through), and the bottom status bar was hardcoded yellow instead of
following the terminal theme. Fixes:

- **Cwd contract** — the TUI's `_launch_configured_cwd()` honored
  `terminal.cwd` from config even for the local backend; the classic CLI
  already forces the spawn directory there. Now backend-aware
  (`utils.is_local_terminal_backend`): local = `os.getcwd()`, non-local
  (ssh/docker) keeps the config value. Root cause of the report: an ambient
  `SON_OF_ANTON_HOME=/var/lib/son-of-anton/.son-of-anton` in the user's
  graphical session (exported in the shell that launched niri, then imported
  into the systemd user manager) pointed the TUI at the gateway service's
  config (`terminal.cwd: /var/lib/son-of-anton/workspace`). `/etc/nixos`
  does NOT export it (`addToSystemPackages` stays false by design). The code
  now defends against the polluted env, but the env itself should be unset
  (and e-play's own `~/.son-of-anton` has no config.yaml, so `setup` is
  needed after unsetting).
- **Rebrand tail** — dropped "by Nous Research" from `DEFAULT_AGENT_IDENTITY`,
  `SON_OF_ANTON_AGENT_HELP_GUIDANCE`, `DEFAULT_SOUL_MD`, the compact-banner
  footer, and the TUI branding (`TAG_*` + the model-suffix); the dead docs
  URL `son-of-anton.nousresearch.com` now points at the fork's repo; the
  `son-of-anton` SKILL.md provider claim corrected to the fork's real
  surface (deepseek, openai-api, custom local endpoints — not "any
  provider... and 20+ others"); the gateway's seeded `SOUL.md` (exact old
  template, zero customization) upgraded in place.
- **Update-notice leak** — the deferred notice printed Rich-rendered ANSI
  from a daemon thread straight to stdout, which prompt_toolkit's
  `patch_stdout` mangles into literal escape garbage (the `?[1;33m` the
  user saw). It now renders Rich markup → 16-color ANSI →
  `_format_update_notice_ansi`, delivered through `_cprint` (thread-safe,
  ANSI-parsed) via a new `deferred_print` callback on `build_welcome_banner`.
- **Emoji strip extended** — new shared `utils.strip_decorative_glyphs`
  (strips emoji blocks, dingbats ✓✗✦✨, misc symbols ⚠⚡⛔, clocks ⏱⏲⏳, VS16;
  keeps the ⚛ brand mark, geometric shapes ◈◉░, kaomoji, arrows). Wired at
  the print funnels — `_cprint` (with `strip=False` for streamed model
  content: glyph stripping is for UI chrome, never the agent's own words),
  `_console_print`, `AIAgent._safe_print`, `_emit_status`/`_emit_warning`,
  gateway tool-progress chrome, delegate spinners, `display._wrap` — plus
  the remaining static strings in the status bar, tips, onboarding,
  conversation_loop, and banner. Gateway reply catalogs were already clean.
- **Status-bar theming** — prompt_toolkit style strings now snap to the
  ANSI-16 palette (`skin_engine.snap_pt_style_to_theme`: fixed RGB names →
  `ansi*`, hexes → nearest palette color), so the status bar, menus, and
  prompt chrome follow the terminal theme like the rest of the CLI instead
  of hardcoded true-color yellow/navy. Skin overrides added for
  `status-bar-session-title` and `status-bar-yolo` (previously hardcoded in
  the CLI's base style).

Tests: Python suite now 80 tests (glyph-strip + backend helpers, skin
snapping contracts, identity no-Nous invariant); TUI 1703 pass + tsc clean;
`nix flake check` green.

### Gateway routing round (2026-08-24, this session)

- **SearXNG 404** (`dfcd763a`) — the provider joined `SEARXNG_URL` with
  `/search` unconditionally, so the e-desktop config
  (`http://10.0.0.6:8888/search`) produced `/search/search` and every
  web_search 404'd. The join now accepts both a bare instance root and the
  full endpoint path (contract test).
- **Aux chain + title scope** (`e301d230`) — the aux auto-chain no longer
  probes the removed OpenRouter/Nous providers (the "marking openrouter
  unhealthy" / "no Nous authentication" noise on every aux call). Step 1's
  custom endpoint now recovers the configured `custom_providers` key
  (`key_env`) when the runtime carries a base_url but no key. The auto-title
  daemon thread ran outside the turn's profile secret scope, so credential
  reads failed closed under multiplexing and every gateway title died — the
  thread now re-installs the scope captured on the turn thread.
- **/help volume** (`e301d230`) — gateway /help shows a 23-line core set
  (`GATEWAY_HELP_CORE`) and points at the paginated /commands for the rest
  (was 59 lines); the CLI /help folds aliases onto their canonical line.
- **Picker key_env** (`76b92591`) — `key_env` conventionally names a
  credential in `~/.son-of-anton/.env`, but the picker read only the process
  environment, so custom endpoints were probed unauthenticated and the
  picker degraded to the single configured default. Now reads via
  `get_env_value` (.env first, scope-aware environment fallback).
- **Deployment** (uncommitted in `/etc/nixos`): the repo's Home Manager
  module is wired for e-desktop's interactive accounts (`interactive.enable`)
  — settings deep-merged into each `~/.son-of-anton/config.yaml`, `.env`
  from the users-group litellm agenix secret, per-user `SON_OF_ANTON_HOME`
  export (kills the ambient gateway-home hijack). Defaults: gateway →
  `gemma-4-26B-A4B-it`, interactive → `qwen3.8-27b-coding`; the custom
  provider declares the 7-model litellm catalog so pickers show it without a
  live probe. Session titles route to the tiny always-resident `supra-title`
  model on oracle through litellm (`auxiliary.title_generation`; litellm
  gained a `supra-title` entry → `10.0.0.6:8080/v1`, oracle's llama-swap is
  now LAN-exposed, `supra-router` removed). The llama-swap swap-matrix
  module no longer treats same-device models as alternatives — every
  non-solo combination may co-reside (gemma + qwen3.6 together on ROCm2,
  qwen3.8 split across ROCm0/1, all simultaneously); VRAM at load time
  decides swaps.

### Outstanding bugs (2026-08-25 — handoff notes for a fresh context)

The 2026-08-24/25 rounds landed the HM per-user config on e-desktop + the
supra-title routing on oracle (both rebuilt, flake lock at `114a8483`).
What the user is still hitting:

1. **Unreadable `$SON_OF_ANTON_HOME/.env` crash — FIXED (`a8e2…`, this round).**
   Repro: `su e-play` from e-work's shell (plain `su` preserves the env, so
   `SON_OF_ANTON_HOME=/home/e-work/.son-of-anton` carried over) → the HM
   activation writes `.env` mode 0600 owned by e-work → e-play's CLI raises
   `PermissionError` at `son_of_anton_cli/env_loader.py:494`. During this
   round's investigation the same crash class surfaced at
   `agent/skill_bundles.py` (`.exists()` on `skill-bundles`, `_max_mtime`)
   and `get_container_exec_info()` (`.container-mode` read). Fix:
   - `load_son_of_anton_dotenv` now checks env paths through
     `_env_file_exists()`, which treats an unreadable file (or an
     untraversable parent — `Path.exists()` raises PermissionError there,
     verified against Python 3.12.14) as absent and logs one warning per
     path. Applied to `.env`, `.op.env`, project `.env`, and the managed
     `.env`.
   - `agent/skill_bundles._iter_bundle_files` / `_max_mtime` and
     `config.get_container_exec_info` fail open the same way.
   The user-side hygiene still applies: `su - e-play` (login shell resets
   the env) and never export the var across accounts. A fully unreadable
   home can still surface permission errors later (session/state writes)
   — the guard removes the crash-on-startup noise, it does not make a
   cross-user home a valid workspace.

2. **Working dir — ACTUALLY resolved 2026-08-25 (round 3). The two previous
   "fixed" claims were both wrong; this one has a mutation-tested regression
   test (`tests/test_cwd_contract.py`).**

   Root cause (confirmed by repro, not inference): the first agent turn
   lazy-imports `gateway.run` (`agent/relay_runtime._segments_config`), whose
   module-level config→env bridge re-bridges `terminal.cwd` over the CLI's
   launch-dir contract. That much round 2 got right.

   **Why round 2's fix (`50df8059`) was a no-op:** it gated the bridge on
   `_SON_OF_ANTON_GATEWAY` — but `gateway/run.py` sets that marker *itself* at
   line ~1918, i.e. at import, before the gate reads it at line ~2184. The
   marker is therefore always `"1"` in **any** process that imports the module,
   CLI included. The gate was a tautology and `TERMINAL_CWD` was still
   clobbered. Verified: with `50df8059` checked out, a CLI-shaped import turns
   `TERMINAL_CWD=/launch/dir` into `/home/e-work`.

   **The fix:** a new marker `_SON_OF_ANTON_GATEWAY_PROC`, set by the gateway
   *launchers* (`cli.py --gateway`, `son_of_anton_cli/gateway.py`) immediately
   **before** they import `gateway.run`, plus `__name__ == "__main__"` for
   `python -m gateway.run`. An incidental import can never set it.
   `_IS_GATEWAY_PROCESS` gates both the config→env bridge and the
   placeholder-cwd resolver.

   The bridge deliberately still runs at **import** time (not from
   `start_gateway()`): module-level consumers capture some bridged vars when
   they are imported — `agent/redact.py` reads `SON_OF_ANTON_REDACT_SECRETS`
   at its own import — so deferring the bridge would silently drop
   `security.redact_secrets`. Gating on process identity keeps the gateway's
   timing byte-for-byte as it was.

   Also fixed in the same area (regressions in the uncommitted WIP refactor
   that preceded this round): `_SkipGatewayBridge` was referenced but its
   class definition had been deleted (`NameError` on the fail-open path); the
   gateway's placeholder-cwd resolution block had been deleted outright
   (`terminal.cwd: auto` left `TERMINAL_CWD` unset); `_cfg` had become a
   function local while module-level IPv4 code still read it; and the bridge
   call sat above `start_gateway`'s docstring, blanking `__doc__`.

   History (kept for context): the `114a8483` local-backend guard and the
   round-1 dotenv reload protection were both correct but insufficient —
   neither covered the gateway-bridge import. `_load_dotenv_with_fallback`
   still snapshots/restores `TERMINAL_*` around `override=True` loads.

3. **Titling garbage (fixed in code + infra, user hasn't confirmed).**
   supra-title looped/truncated and the raw `{"title" : "Response Response
   …"}` got persisted. Guards landed (`114a8483`: truncated-JSON salvage,
   degenerate/repetition rejection) and oracle's supra-title runs greedy
   (`--temp 0`). Existing garbage titles in `state.db` stay until retitled
   (`/title <name>` or `/new`).

### TUI removed (2026-08-25, this round — "cli is all i want. kill tui!")

> **Superseded.** This removed the `ui-tui/` TypeScript app. A Textual
> front-end was built in its place on 2026-09-01, and `9906a30b` then deleted
> the prompt_toolkit REPL — so the conclusion below ("the CLI is the only
> interface") is now exactly inverted. See "Textual front-end" below.

- `ui-tui/`, `tui_gateway/`, `scripts/profile-tui.py` deleted (501 tracked
  files). Root cause of the flag's failure: the Nix package (son-of-anton.nix)
  explicitly excludes `ui-tui` from the build and never shipped the esbuild
  bundle, but `main.py` still offered `--tui` — whose recovery text told the
  user to `git restore -- ui-tui` INSIDE a store path. That could never work.
- `--tui`/`--tui-dev` and `SON_OF_ANTON_TUI=1` are gone from both parsers;
  `son-of-anton --tui` now errors cleanly ("unrecognized arguments"). The
  default is and stays the classic prompt_toolkit CLI.
- `main.py` TUI machinery removed (early-interface probe, TUI argv/npm
  workspace provisioning, `_launch_tui`, the Termux TUI fast path, `--dev`);
  `_resolve_continue_arg` and `cmd_chat` simplified to source="cli" only.
- `tui_gateway` dropped from pyproject packaging, the venv-import sweep
  (nix/checks.nix), the MCP child-spawn markers, update's npm workspace
  list, the module-logging component map, file-safety markers, and
  doctor's npm audit targets. `_capture_gateway_steer_authority` is now an
  explicit no-op bridge (the TUI live-steer host is gone).
- Remaining `tui_gateway`/`ui-tui` mentions in comments are history only.

### Nix-only sweep (2026-08-25 — "the more dead code we kill, the easier it is to find bugs")

Son of Anton now supports ONLY Nix (NixOS/Home-Manager, Linux + macOS).
Every other OS/environment fallback has been stripped from the product
surface: ~11k lines, 87 files, three delegated waves + the lead's fixups.

- **Windows**: removed `sys.platform == "win32"`/Windows/os.name=="nt"
  branches, msvcrt/pywin32 lock paths, .exe/pythonw/Scripts handling,
  MSYS/Git-Bash path translation, taskkill, PowerShell, Windows Terminal
  shims, winreg, the pywin32/tzdata/pywinpty/concurrent-log-handler
  dependencies (uv.lock regenerated — 4 deps gone), and the standalone
  `gateway_windows.py`, `_scan_venv_blockers.py`, `setup-son-of-anton.sh`.
- **Termux**: all Termux detection, fast-launch paths, install wording,
  and the `_startup_fast` helpers (`is_termux_env`, termux version path).
- **Docker/s6/container**: `_is_container`/`/.dockerenv`/cgroup detection,
  `.container-mode`/`get_container_exec_info`, `container_boot.py`,
  TERMINAL_DOCKER_* terminal backends (local+ssh stay), docker install
  methods + `format_docker_update_message`, s6 supervision/dispatch
  (gateway, doctor, profiles, service_manager, restart, monitoring).
- **venv/pip fallbacks**: `.venv`/`venv` probing, `_scan_venv_blockers`,
  pip-based install/update messaging (uv + Nix flows stay), and
  `scripts/run_tests.sh` venv probing (Nix dev shell only now).
- Kept: macOS/darwin, NixOS/Home-Manager modules, systemd/launchd service
  code, managed scope, SSH terminal backend, `terminal.home_mode`,
  git-checkout dev/update flow. `detect_install_method` now returns only
  nix/nixos/home-manager/git/unknown.
- Gates after the sweep: 94 tests pass, `nix flake check` green (sealed
  venv import sweep covers the edited modules). Cross-file breakage found
  and fixed by review: `tools/file_operations._escape_shell_arg` (was
  importing removed `_bash_safe_path`), `cmd_update`'s dead
  `format_docker_update_message` import, `gateway/cwd_placeholder`'s
  docker branch, `service_manager`/`doctor`/`profiles` s6 paths, and the
  `venv_python_path` windows kwargs.

---

### Hermes-vs-fork attribution (the answer to "there's no way hermes is this bad")

The user's premise is verified BOTH ways — today's crash is NOT fork-caused,
but the worst turn-class bugs ARE:

- **Upstream, verbatim:** the unreadable-`.env` crash. Upstream
  `hermes_cli/env_loader.py` at `fcbd1076a9` is byte-identical in the
  `if user_env.exists():` handling (the initial fork import
  `677b5c39` → `be970450` only renamed `hermes`→`son-of-anton`). The fork
  amplified exposure: per-user profiles + HM 0600 `.env` + `su` across
  accounts is a deployment shape upstream never had. Likewise the whole
  `terminal.cwd` → `TERMINAL_CWD` bridge + `_reapply_terminal_config_bridge`
  re-run on every dotenv load is upstream architecture — the `114a8483`
  local-backend guard was a partial fork fix to an upstream contract
  conflict (CLI: spawn dir; gateway: config cwd; same env var).
- **Fork-caused, by surgery:** the live-round crashes — credits-straggler
  NameError/AttributeError (removed subsystem, orphaned calls), the i18n
  empty-key strings (locale prune deleted catalogs but left call sites),
  the sealed-venv `import plugins` failure (wrapper never set PYTHONPATH),
  the `_resolve_session_agent_mode` `.get()` AttributeError (new fork
  router), dict-form `custom_providers` rejection (fork validation vs
  upstream examples). These are the bugs that made the user want to quit;
  they are all strip/rewrite artifacts, not hermes quality.
- **Fork packaging:** the TUI `--tui` failure (ui-tui excluded from the Nix
  package while the flag stayed; now removed) and the Nix-only deployment
  complexity (multiplexed profiles, terminal.home_mode=cwd, HM config
  deep-merge) that produced the cwd/`~`/env-pollution bugs — none of which
  exist upstream.

The /etc/nixos changes from the round are committed there; the fork is at
`114a8483` on `main` (pushed).

### Provider + dead-code sweep (2026-08-25 → 09-01)

The largest arc since Phase 7: roughly 40 commits whose only theme is
"delete what nothing calls". Providers cut to the three this fork actually
ships (custom / openai-api / deepseek-via-litellm): the native Gemini wire
(`dfdc225c`), the Anthropic Messages wire (`ba850a9e`) and its `api_mode`
(`801d46d5`), the Copilot ACP client + OAuth flow (`a9563978`), Nous Portal
(`66e16084`) and its deep tail (`2b843a4d`), Qwen Portal (`fb54ddbf`), the
direct DeepSeek API (`c355fae5`), AWS Bedrock + Azure Foundry (`092ed9e4`),
Vertex AI + google-workspace deps (`6d284990`), and the vendor-host header
layer (`c1c5bb53`). Subsystems: 17 modules nothing imported (`b8d2a395`),
the Kanban worker (`2424f0e6`), the codex app-server runtime (`a15bcd29`),
the automation blueprint/suggestion subsystem (`c089d5ab`), upstream
community + JS scaffolding (`36a0cb57`, 23.5k lines), and the profiles
system + gateway multiplexing (`2067d1be` + `292ab53e`, 8k lines).

The lesson worth keeping: `e8a3e7dc` added a test that catches dead
references the interpreter never sees, and it immediately found more
(`53375a2b`, `0836fe88`) — an import sweep only proves a module loads, not
that anything calls into it.

### Multi-instance gateway (2026-08-26)

Profiles were the wrong shape: one process multiplexing several identities.
`7e79f545` replaced them with one systemd service per account, each with its
own `SON_OF_ANTON_HOME`, working directory and model. `39bc6c16` added
`gateway.model` so a surface's default comes out of one config.yaml;
`5b6220ab` added `router.modes` so an instance can switch physics/research
off; `c57573c3` swapped the ACL allowlist for a kernel-enforced credential
denylist; `73f4116c` + `309b7ae3` + `120bf2dd` finished the Signal DM/home
channel/attachment routing. `8f1e2557` and `abad9565` gave an instance
active hours — turns held outside them, then summarized when the window
opens.

### Physics round (2026-08-31)

`2e7e694e` sandboxed computation, added problem specs and MCP lookups, and
split coder/critic models. `f0fe99cd` made the run read `ROOT` the way the
run itself will, and stopped reporting unscoreable results as scored.
`a55eb8eb` added an outside critic, per-role models, and a Manager that can
read its own workspace.

### Textual front-end (2026-09-01 → 09-02)

The interface question reopened, and the answer inverted the 2026-08-25
round. `35b0745c` proved a Textual spike; `674b92a7` grew it into a chat-app
layout; `8bc898e5` shipped Textual via nix and wired `--tui`; `04f54890`
ported opencode's session frame (right panel > 120 cols, multi-line prompt,
wordmark, `:q`). Then `9906a30b` deleted the prompt_toolkit REPL outright.

Design constraint that must survive: `son_of_anton_tui/backend.py` defines
`TextualBackend(SonOfAntonCLI)` and overrides only rendering seams and the
queue-answering modal prompts. The agent loop, prompt caching and slash
commands are the unchanged `cli.SonOfAntonCLI`. Extend by overriding more
seams, not by touching `cli.py` output paths. Anything that draws its own
screen (an editor) must go through `run_with_terminal()`, which suspends the
app first.

`c4635dac` made `/commit` pin the configured account as **author** only,
leaving the committer as-is, so the log reads "authored by son-of-anton-bot,
committed by me".

### Textual front-end — bug round (2026-09-02, UNCOMMITTED)

Six defects, all found by using the thing. Four were live bugs:

1. **A `[` in model prose killed the app.** `Static.update()` runs Textual's
   content-markup parser by default, so one bracket in a reasoning stream
   raised `MarkupError` out of the render and ended the session mid-turn.
   Every Static showing text this codebase did not author is now
   `markup=False`: the reasoning body, modal titles and details (a
   tool-approval title carries shell commands), and the sidebar's session
   title and cwd.
2. **Steering mid-turn always failed.** The interrupt itself worked — see
   `~/.son-of-anton/interrupt_debug.log`. But `chat()` then took its
   non-streamed branch and rendered the interrupt notice through
   `_render_final_assistant_content` → `realign_markdown_tables`, which
   imported `wcwidth` — a prompt_toolkit transitive that left the tree with
   the REPL. The import fires before the function looks for a table, so
   *every* steer died with `Error: No module named 'wcwidth'`, table or no
   table. Widths now come from `rich.cells` (a core dep, and more accurate:
   `wcswidth` returned -1 for emoji with a variation selector).
   Pinned by `tests/test_markdown_tables.py`, including a test asserting
   `import wcwidth` still fails so the fix can't quietly stop mattering.
3. **No prompt history.** Deleting the REPL took prompt_toolkit's
   `FileHistory` with it and nothing replaced it, so ↑ did nothing. New
   `PromptHistory` reads and appends the same
   `~/.son-of-anton/.son_of_anton_history` in the same format, so history
   from before the interface swap is still under the arrow key. ↑ still
   moves the cursor inside a multi-line draft and only recalls off the top
   row.
4. **Mouse selection stole the prompt.** Textual focuses whatever focusable
   widget you press on, so a click or drag in the transcript left the caret
   stranded. Focus is handed back on mouse-up; selections live on the screen,
   not the focus, so the highlight survives.

Plus: drag-select auto-copies with a toast (terminals do this natively and
Textual's mouse capture takes it away), `ctrl+shift+v` bound to paste for
terminals that forward the chord instead of swallowing it, and the status
row grows instead of clipping the "waiting Ns with no output" heartbeat.

Also removed in this round: **`/prompt`** (ctrl+g already opened the draft in
`$EDITOR` and sent it; `/prompt` was the same thing one keystroke slower),
and the **model-slot half of the router** — `classify_complexity()` /
`resolve_model_slot()` plus six `router.*_model` config keys, ported from
temple, given a config surface and tests, and never wired to model
resolution. 80 of 257 lines in `router.py`. The mode half is live and
load-bearing: four production call sites, and four of the five deployed
gateway instances set `router.modes = [ "standard" ]`.

### Nine-agent research pipeline removed (2026-09-03, UNCOMMITTED)

The `research` mode — physics-intern's nine-agent critical self-research
pipeline (surveyor → planner → orchestrator → researcher/computer →
reviewer → critic → adjudicator → formatter over a shared
`ResearchState`) — was removed as redundant with the Autophysicist, which
already carries its own critic, sub-agent dispatch, and verdict-free
review. Only `standard` and `physics` remain.

Deleted: `physics_intern/engine.py`, `physics_intern/agents/`,
`physics_intern/state/`, `physics_intern/control/`,
`physics_intern/rendering/`, `physics_intern/utils/categories.py`,
`physics_intern/verification/workspace.py`,
`physics_intern/verification/event_summary.py`. The `WorkspaceManager`
class (engine-only) went with it; its safety guard was extracted to
`physics_intern.core.workspace.assert_safe_workspace_root()` and the
physics runner now calls it before `git init`.

`son_of_anton_cli/router.py` lost its `research` keywords and the
`AGENT_MODES` / `classify_mode` research arms; the router is now
`standard` / `physics`. `physics_intern/run.py` dropped the
`--mode research` branch; the CLI, gateway, `config_defaults.py`,
`config.default.yaml`, `mcp.py` roles, and `subagent.py` were all
re-anchored on the two-mode model. Tests were rewritten against the
surviving surface (router, physics_parity, workspace, problem_spec,
sandbox) rather than deleted. Full suite green: 47 files, 600 tests.

Docs updated: README (mode list, provenance, physics runs, `/mode` table,
`router.modes` example), AGENTS.md (router, physics mode, CLI/Interface),
STATUS.md (this entry). The provenance prose now names the pipeline as
ported-then-removed rather than a live divergence from physics-intern.

## Current state / known loose ends

- **Deployed live on e-desktop** (see `/etc/nixos/hosts/e-desktop/configuration.nix`):
  **five gateway instances**, one systemd service per account —
  `work` → `/home/e-work`, `play` → `/home/e-play`, `house` → `/srv/household`,
  `ricky` → `/srv/ricky`, `markets` → `/srv/markets`. All five run
  `qwen3.8-27b-coding`. Only `work` gets both agent modes; the other
  four pin `settings.router.modes = [ "standard" ]`, which is what that knob
  exists for. Supporting infra: litellm on oracle (`10.0.0.6:4000`, pinned to
  nixpkgs `ced43465` — nixpkgs' litellm 1.97 is broken, missing the
  `expression` dependency); SearXNG on oracle; signal-cli in HTTP mode on mu
  with `--send-read-receipts` (read receipts work; the 👀/✅ reaction set is
  off via `SIGNAL_REACTIONS=false`; typing indicators off). Session titles
  come from `supra-title` on oracle via litellm.
  Verified 2026-09-02: litellm answers `qwen3.8-27b-coding` in ~0.7s, served
  by **vllm** (`vllm-0.28.0-tp2`), not the llama-swap this section used to
  name. The instance/ACL details beyond the file above were not re-verified
  this round.
- **Deep-Nous tail excised** (this session). `nous_account.py`, `agent/portal_tags.py`,
  root `models.py`, and the proxy's `nous_portal.py` adapter were gone already; this
  pass removed the remaining credential-gated Nous surface:
  - `son_of_anton_cli/auth.py` — the `NOUS_*`/`DEFAULT_NOUS_*` constants, the Nous
    host-allowlist frozensets, `_NOUS_EFFECTIVE_STATE_IGNORED_KEYS`, the "Nous Portal
    token refresh/model discovery" section (shared token store, `_refresh_access_token`,
    the `_RESOLVE_TOKEN_CACHE_*` memo), the `get_nous_auth_status` cache
    (`_NOUS_AUTH_STATUS_CACHE_TTL`/`_nous_auth_status_cache`), the `NOUS_SESSION_*`
    enums, the `get_auth_status` `"nous"` dispatch arm, and the `DEFAULT_NOUS_PORTAL_URL`
    upgrade-footer path.
  - `agent/auxiliary_client.py` — the Nous routing arms in `resolve_provider_client`
    and the vision/OAuth resolvers, `auxiliary_is_nous`, the `_try_nous` / `_nous_extra_body`
    / `_nous_portal_tags` dead calls, the deleted-module `agent.portal_tags` import,
    the Nous self-heal + auth-refresh blocks in both `_call_llm_impl` and
    `_async_call_llm_impl`, `get_auxiliary_extra_body`, `_AUTO_PROVIDER_LABELS`, and the
    nous host/alias/`_VISION_AUTO_PROVIDER_ORDER`/`_AUX_UNHEALTHY_LABEL_ALIASES` entries.
  - `agent/model_metadata.py` — `_resolve_nous_context_length` and the
    `effective_provider == "nous"` branch; `agent/chat_completion_helpers.py` — the
    nous-only fallback-entry check; `tools/vision_tools.py` + `tools/delegate_tool.py` —
    the nous provider entries/help text.
  - Plus the unused imports flagged by vulture and an unreachable block in
    `run_agent.py`.
  `agent.aux_accounting` is live (imported by the aux client and title generator) and was
  left in place; `openrouter` remains a live provider (used by `tools/openrouter_client.py`).
  Stale prose still names `resolve_nous_access_token()` in the gateway relay's enroll
  docstring (never called in code). `doctor.py` still carries inert removed-provider
  probes (Nous auth row).
- **Physics mode live-smoke-tested** (`012bd9df`) against llama-swap
  (`qwen3.6-35b-a3b` @ `http://10.0.0.5:8080/v1`). The run solved
  bromine_halflife end-to-end (workspace → RESULTS.txt → FORMAL_EVAL.md,
  3/3 PASS with halflife 119.279s vs 119.2 true on the first solve).
- **Standard mode live-tested via Signal** — the full loop (pairing, home
  channel, /reset, agent turns through litellm) works; the bot answered real
  questions after the fix round above. Tested when the gateway still
  multiplexed profiles; the per-instance replacement (`7e79f545`) has not had
  the same end-to-end pass.
- **Research mode was destructive until 2026-08-25 (round 3) — now fixed.**
  It had never been run, which is why nobody caught it. `Config.workspace_dir`
  defaults to `""` → `Path(".")` → **the process cwd**, and both entry points
  (`cli._run_research_mode`, `gateway._run_physics_mode_sync`) constructed
  `PhysicsIntern(message)` with no config. `WorkspaceManager.init()` then ran
  `git init` + `git add -A` + `git commit` **in the cwd** — the profile's HOME
  under the gateway, the user's project under the CLI. Demonstrated: a routed
  message ("derive the ...") staged `.ssh/id_ed25519` and personal documents
  into a new repo. It also fired for real during this session's investigation,
  committing to the repo's own `main`.
  Fixes: `physics_intern.core.workspace.resolve_workspace_root()` returns a
  fresh **absolute** root under `~/.son-of-anton/workspaces` (override:
  `physics.workspace_root`); both entry points pass an explicit `config=`;
  the autophysicist's old relative `workspaces/...` default is absolute too;
  and `WorkspaceManager._assert_safe_workspace_root()` refuses a relative
  path, an existing git repo, or `$HOME`/`/` as defense in depth.
  Covered by `tests/test_physics_workspace.py`. The destructive-path fix is
  verified; end-to-end model behaviour never was. **Superseded:** the
  research mode has since been removed entirely (see below) — only the
  workspace-safety invariants survive, now exercised by the physics runner.
- **Physics runs are synchronous turns** — a session blocks until the run
  finishes; the priority queue is the eventual fix.
- **Textual is the only interface** — `cli.main()` builds a `TextualBackend`
  unconditionally and hands it to `son_of_anton_tui.tui.run_app()`. There is
  no `--tui`/`--cli` flag and no fallback REPL: if Textual will not start,
  nothing interactive starts. Single-query mode (`son-of-anton "..."`) still
  bypasses the front-end entirely.

### `_SON_OF_ANTON_GATEWAY` made truthful (2026-08-25, round 3)

The marker was set **unconditionally at `gateway/run.py` import**, so any
process that imported the module — the CLI does, lazily, on its first agent
turn — looked like a gateway to every consumer that reads it. Four consumers
were silently mis-firing in ordinary CLI sessions:

| consumer | effect when wrongly true |
|---|---|
| `cli.py:659` | CLI skips force-exporting its launch dir to `TERMINAL_CWD` |
| `tools/terminal_tool.py:2236` | agent refuses `systemctl restart son-of-anton-gateway` |
| `son_of_anton_cli/gateway.py:6207` | `son-of-anton gateway stop` refused as self-targeting |
| `son_of_anton_cli/gateway.py:6273` | `son-of-anton gateway restart` refused as self-targeting |

So after one agent turn, an ordinary CLI session could no longer stop or
restart its own gateway. (`tools/process_registry.py` was already immune: it
additionally requires PID-file ownership, and its docstring called out this
exact hazard.)

The assignment is now gated on `_IS_GATEWAY_PROCESS`, defined **before** it
from `_SON_OF_ANTON_GATEWAY_PROC` (set by the launchers) — so the variable
finally means what all five consumers already assumed: *"I am in the gateway
process tree."* No consumer's logic changed; only the truth of the input.
Export/inheritance semantics are unchanged, so gateway children still see it.

The detached restart watcher now sheds **both** markers (it shed only
`_SON_OF_ANTON_GATEWAY` before); otherwise it would re-mark itself as a
gateway on importing `gateway.run` and its `gateway restart` would be refused
silently, leaving the gateway down — the exact failure the original pop
existed to prevent.

Covered by `tests/test_cwd_contract.py` (10 tests, mutation-tested).

### Test-suite blind spot (2026-08-25, round 3)

Both bugs above were invisible to a 96/96-green suite, for the same reason:
**the suite only tested things that are cheap to test in-process.**

- The cwd bug is an **import-time, one-shot, cross-process** effect. It cannot
  be observed by importing a module inside an already-running pytest process —
  by then the marker is set and the bridge has fired. `tests/test_cwd_contract.py`
  therefore spawns real subprocesses.
- The research-mode bug lived on a path **nothing ever executed**. Import
  sweeps and signature checks pass happily; only construction reveals it.

Both new test files were **mutation-tested**: the fix was reverted and each
test confirmed to fail, then restored. A regression test that has never been
seen to fail is not evidence.

Rule of thumb going forward: when a fix is about *which process* or *when in
startup* something happens, the test must spawn that process. Everything in
this repo's cwd/env-bridge class needs a subprocess test.

## What's left

1. **Commit the 2026-09-02 bug round.** It is green (621 tests, `ruff` clean)
   but sitting uncommitted in the tree at the user's request.
2. **Research-mode live smoke test** — never driven against a real endpoint.
   The destructive-path fix is verified; end-to-end model behaviour is not.
3. **Docs** — done 2026-09-02: README's inverted interface line is fixed, and
   the "the router picks the model per request" claim is gone from README,
   `/model auto` (cli.py) and the gateway's `/model`+`/mode` replies. The
   router never picked models; the half that claimed to is deleted.
4. **Deep-Nous residue** — stale prose still names `resolve_nous_access_token()`
   in the gateway relay's enroll docstring (never called), and `doctor.py`
   still carries inert removed-provider probes (Nous auth row).
5. **TUI gaps** — image attachments in the prompt and an `/agents` viewer were
   never carried over from the REPL.

## Operational notes

- Commits: author `son-of-anton-bot <307402699+son-of-anton-bot@users.noreply.github.com>`, trailer `Co-authored-by: Ethan Todd <30243637+ewtodd@users.noreply.github.com>` (repo git config already set)
- Remote: `git@github.com:ewtodd/son-of-anton.git`, branch `main`
- Verify: `nix flake check` (package + modules + venv import sweep), full-tree compile via `/tmp/opencode/compile_all.py` (path points at `/home/e-play/Software/son-of-anton`), import sweep via `/tmp/opencode/import_sweep.py` (run inside the sealed venv)
- Python tests: `nix develop -c scripts/run_tests.sh` (621 tests across 47 files, ~32s); hooks: `nix develop -c pre-commit install`
- `son-of-anton` = the Textual app, unconditionally. No `--tui`/`--cli` flag, no REPL fallback.
- Ad-hoc Python: there is no `python` on PATH and `nix develop -c python` is a bare 3.14 without deps. Use the editable venv — `nix develop -c bash -c 'echo $SON_OF_ANTON_PYTHON'` for the current store path, then run it with `SON_OF_ANTON_PYTHON_SRC_ROOT=/home/e-work/son-of-anton` exported. Point `SON_OF_ANTON_HOME` at a scratch dir for backend experiments.
- **Deployment**: the gateway lives in the user's `/etc/nixos` repo (host
  `e-desktop`), input `son-of-anton` following `main` — bump with
  `nix flake lock --update-input son-of-anton` + `nixos-rebuild switch`.
  litellm is pinned via the `nixpkgs-litellm` input (nixpkgs `ced43465`).
  The 2026-08-24 round also touches `hosts/oracle` (llama-swap LAN-exposed
  for the litellm supra-title route) and the home-manager
  son-of-anton module — rebuild both hosts and log out/in on e-desktop so
  the per-user `SON_OF_ANTON_HOME` export and config.yaml apply.
- **Physics smoke test repro** (needs the llama-swap host up):
  temp `SON_OF_ANTON_HOME` with config.yaml → `model: qwen3.6-35b-a3b`,
  `provider: custom`, `custom_providers.custom.base_url: http://10.0.0.5:8080/v1`,
  `physics.model/base_url` set; run `/tmp/opencode/smoke/run_phys.py` in the
  sealed venv with a `python` shim on PATH (compute scripts shell out to
  bare `python`).
- uv (for lock regen): `nix shell nixpkgs#uv -c env UV_PYTHON="$(nix develop -c bash -c 'echo $SON_OF_ANTON_PYTHON')" uv lock` — the store path this used to hardcode has been garbage-collected; never pin one here
- The agent name: the user renamed the GitHub account to `son-of-anton-bot`; do not create a DeepSeek co-author trailer (no such account exists)
