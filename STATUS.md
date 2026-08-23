# Son of Anton — Status & Handoff

Written 2026-08-22 after the fork/build-out session, updated 2026-08-22 after
the Phase 7 cleanup pass and the deep-Nous cleanup layers. Everything below
is the state as of commit `da4b4100` on `main` (pushed to
`github.com:ewtodd/son-of-anton`).

---

## What this is

`son-of-anton` is a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (upstream commit `fcbd1076a9`), stripped to a lean always-on daemon
surface, extended with the physics modes from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`). MIT. It is the successor to the archived
[temple](https://github.com/ewtodd/temple) harness — the daemon design,
permission modes, and request router carry over from it.

Three agent modes, selected per request by a heuristic router with `/mode` override:
- `standard` — the hermes agent loop (terminal, files, web, skills, memory, delegation, cron)
- `physics` — Autophysicist: single research manager, permanent memory + scratchpad, token budget, `submit_final_answer`, git workspace
- `research` — nine-agent critical self-research pipeline (surveyor → planner → orchestrator → researcher/computer → reviewer → critic → adjudicator → formatter)

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
- Kept: CLI, TUI (+apps/shared), gateway core, cron, profiles, curator, memory providers, MCP client, iron proxy, discord tool, terminal backends local+ssh
- Core tools: web_search/extract, terminal, process, file ops, vision_analyze, skills, todo, memory, session_search, clarify, execute_code, delegate_task, cronjob

### Phase 4 — platforms + router + permissions
- Gateway down to Discord (`plugins/platforms/discord/`), Slack (`plugins/platforms/slack/`), Signal (`gateway/platforms/signal.py`)
- `son_of_anton_cli/router.py`: `classify_mode()` (physics/research/standard keywords), `classify_complexity()` (simple/default/complex), config section `router:`
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

---

## Current state / known loose ends

- **User's home skills are stale**: `~/.son-of-anton/skills/` still holds the
  pre-prune 78 skills. One-time: `rm -rf ~/.son-of-anton/skills` (then
   optionally `son-of-anton setup` to install the bundled 43).
- **Remaining deep-Nous tail** (one final pass, all credential-gated dead
  paths — nothing user-visible reaches them):
  - `nous_account.py` + `agent/portal_tags.py` + `agent/aux_accounting.py`
  - auth.py's Nous OAuth flow: portal device-code login, invoke-JWT checks,
    `resolve_nous_runtime_credentials`, `step_up_nous_billing_scope`, and the
    nous model-fetch block (`get_curated_nous_model_ids` callers)
  - `agent/auxiliary_client.py`'s Nous branch (`auxiliary_is_nous`,
    `_NOUS_MODEL`, pool resolution, `_nous_extra_body`)
  - models.py Nous-curated helpers: `check_nous_free_tier`,
    `fetch_nous_recommended_models`, `get_curated_nous_model_ids`,
    `partition_nous_models_by_tier`, `union_with_portal_*`
  - lmstudio runtime-load cluster (`_ensure_lmstudio_runtime_loaded` +
    models.py `ensure_lmstudio_model_loaded`/`fetch_lmstudio_models`) and
    the opencode api-mode helpers in model_switch/runtime_provider/cli
  - the iron-proxy's `nous_portal.py` adapter (kept deliberately — the proxy
    is retained and this is its only OAuth upstream; fails closed without
    legacy auth.json state)
  - doctor.py's removed-provider connectivity probes
  Excising these without E2E coverage of the aux/auth chains is riskier than
  leaving inert gated code — do it with a live smoke test in hand.
- **doctor.py** keeps a few removed-provider probes (Nous auth row,
  connectivity checks); `runtime_provider.py` keeps openrouter host-guards as
  defense-in-depth. Triage with the final pass above.
- **Physics mode live-smoke-tested** (this session, `012bd9df`) against
  llama-swap (`qwen3.6-35b-a3b` @ `http://10.0.0.5:8080/v1`). The run solved
  bromine_halflife end-to-end (workspace → RESULTS.txt → FORMAL_EVAL.md,
  3/3 PASS with halflife 119.279s vs 119.2 true on the first solve). The
  smoke test surfaced and fixed four vendored-wiring bugs: Config raised for
  unregistered models (models.yaml was dropped), build_config dropped the
  `overrides` kwarg, compute scripts ran in `computations/` so RESULTS.txt
  missed the evaluator's read path, and `render_formal_evaluation` imported
  the removed `verification.core` subpackage. Regression tests added.
- **Research mode is NOT yet live-smoke-tested** — the nine-agent pipeline
  runs the same fixed Config/endpoint layer, but hasn't been driven against
  a real endpoint.
- **Physics runs are synchronous turns** — a session blocks until the run
  finishes; the priority queue is the eventual fix.
- **Research pipeline prompts are theory-era text** — experimental-language
  prompt tuning is a follow-up.
- **TUI theming**: `ui-tui/src/theme.ts` still has its own hex color-mix
  engine — converting it to terminal-theme-driven is a bigger JS refactor,
  not started. The TUI also still ships `topup.ts` / `subscription.ts` slash
  commands whose Python RPCs are gone — remove them with the TUI theming pass.
- **TUI-side `:q`**: fixed this session (item 6 above).

## What's left

1. **Final deep-Nous pass** — the tail above. The physics smoke path now
   exists as the regression net, but it doesn't exercise the aux/auth
   chains; add a quick standard-mode + auxiliary-call smoke before excising.
2. **Research-mode live smoke test** — same llama-swap endpoint.
3. **TUI follow-up** — remove topup/subscription commands; theming decision
   (keep the hex engine or port terminal-theme colors).
4. **README** stays in sync with the above.

## Operational notes

- Commits: author `son-of-anton-bot <307402699+son-of-anton-bot@users.noreply.github.com>`, trailer `Co-authored-by: Ethan Todd <30243637+ewtodd@users.noreply.github.com>` (repo git config already set)
- Remote: `git@github.com:ewtodd/son-of-anton.git`, branch `main`
- Verify: `nix flake check` (package + modules + venv import sweep), full-tree compile via `/tmp/opencode/compile_all.py` (path points at `/home/e-play/Software/son-of-anton`), import sweep via `/tmp/opencode/import_sweep.py` (run inside the sealed venv)
- Python tests: `nix develop -c scripts/run_tests.sh` (52 tests, <1s); hooks: `nix develop -c pre-commit install`
- TUI tests: `ui-tui/` — `npm run build:ink && npm test` (node via `nix shell nixpkgs#nodejs_22`)
- **Physics smoke test repro** (needs the llama-swap host up):
  temp `SON_OF_ANTON_HOME` with config.yaml → `model: qwen3.6-35b-a3b`,
  `provider: custom`, `custom_providers.custom.base_url: http://10.0.0.5:8080/v1`,
  `physics.model/base_url` set; run `/tmp/opencode/smoke/run_phys.py` in the
  sealed venv with a `python` shim on PATH (compute scripts shell out to
  bare `python`).
- Python for ad-hoc checks: `/nix/store/sgr5qv39ji4gddv37jw1iw069gqxa0x2-python3-3.12.14/bin/python3.12` (bare) or the sealed venv's `bin/python3` (has deps)
- uv (for lock regen): `nix shell nixpkgs#uv -c env UV_PYTHON=/nix/store/sgr5qv39ji4gddv37jw1iw069gqxa0x2-python3-3.12.14/bin/python3.12 uv lock`
- The agent name: the user renamed the GitHub account to `son-of-anton-bot`; do not create a DeepSeek co-author trailer (no such account exists)
