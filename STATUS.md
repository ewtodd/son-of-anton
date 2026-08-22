# Son of Anton — Status & Handoff

Written 2026-08-22 after the fork/build-out session. Everything below is the
state as of commit `e2c63433` on `main` (pushed to `github.com:ewtodd/son-of-anton`).

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
- New history: 4 commits authored by `son-of-anton-bot` (307402699+son-of-anton-bot@users.noreply.github.com), co-author `Ethan Todd`
- Full mechanical rename: binary `son-of-anton`, package `son_of_anton_cli`/`son_of_anton_*`, env `SON_OF_ANTON_*`, home `~/.son-of-anton`, npm scope `@sonofanton/*`
- History was rewritten (filter-branch) twice: dropped the DeepSeek co-author trailer, then renamed renco→son-of-anton

### Phase 3 — bloat strip
- Removed: Electron desktop app, web dashboard + `web_server.py`, docs website, ACP adapter, docker, native CJK FTS, i18n locales, optional-skills/mcps, research/trajectory tooling, ~25 plugins, 37→3 model providers (custom/openai/deepseek), browser/TTS/voice/wake/video/image-gen/HA/kanban/computer-use/x_search/feishu/yuanbao tools, MoA core, pets, CI, ~half the tests
- Kept: CLI, TUI (+apps/shared), gateway core, cron, profiles, curator, memory providers, MCP client, iron proxy, discord tool, terminal backends local+ssh
- Core tools: web_search/extract, terminal, process, file ops, vision_analyze, skills, todo, memory, session_search, clarify, execute_code, delegate_task, cronjob

### Phase 4 — platforms + router + permissions
- Gateway down to Discord (`plugins/platforms/discord/`), Slack (`plugins/platforms/slack/`), Signal (`gateway/platforms/signal.py`)
- `son_of_anton_cli/router.py`: `classify_mode()` (physics/research/standard keywords), `classify_complexity()` (simple/default/complex), config section `router:`
- `/mode auto|standard|physics|research`, `/model auto|NAME` (auto clears the session pin incl. the persisted store override), `/perm default|ask|lockdown|yolo` mapped onto the approval core (`security.lockdown` forces every command through human approval, beats yolo/allowlist/smart-approve)
- `:q`/`q` quit (was aliased to `queue` in hermes — fixed); command resolution accepts `:` prefix

### Phase 5 — Nix packaging + per-user daemons
- `nix build .#default` and `nix flake check` fully green
- Python-only package: sealed uv2nix venv + wrapper exposing bundled skills/plugins (SON_OF_ANTON_BUNDLED_SKILLS/_PLUGINS)
- Home Manager module = per-account daemon (one gateway per work/play account, own `~/.son-of-anton`; set `users.users.<name>.linger = true`)
- NixOS module (system service, dedicated user) also available; container mode + serve/dashboard backend units removed
- pyproject extras pruned (messaging = discord+slack; `[all]` = messaging+cron+pty+mcp+honcho+hindsight+google); uv.lock regenerated
- venv-imports check imports the full surface (18 modules incl. both physics modes) — catches import breakage compileall can't

### Phase 6 — physics modes
- `physics_intern/` vendored at repo root; CLI/providers/baselines/sympy-checker/models.yaml dropped
- New `physics_intern/llm.py`: OpenAI-compatible layer resolving endpoints from config.yaml (`physics.base_url` → deepseek/openai defaults → `custom_providers` → `127.0.0.1:8080/v1`), retry + context-length detection
- Autophysicist refactored into callable `run_autophysicist(...)`
- Experimental verification (`physics_intern/verification/experimental.py`): numeric `checks` against a problem spec, workspace `RESULTS.txt` (`key = value`), optional checker script, `FORMAL_EVAL.md`
- Toy problems in `problems/`: cobalt_calibration (60Co peak fit), bromine_halflife (decay fit), yap_psd (tail-to-total PSD) — data synthesized by the model (real UM-ANSG data is private)
- Wired into CLI chat() and gateway turns (ack → worker-thread run → ANSWER + eval delivered back); wheel ships physics data files via package-data

### Theming (kitty)
- Banner ASCII art is plain text; default skin foreground colors are ANSI names; `_hex_to_ansi` snaps custom-skin hex to the legacy-16 palette; rich Consoles use `color_system="standard"`; diff display + streamed text palette-based; caduceus ⚕ replaced everywhere with the ⚛ atom

### Version
- v0.2.0 (temple was 0.1.0), release date 2026.8.22

---

## Current state / known loose ends

- **User's home skills are stale**: `~/.son-of-anton/skills/` still holds the pre-prune 78 skills. One-time: `rm -rf ~/.son-of-anton/skills` (then optionally `son-of-anton setup` to install the bundled 43).
- **Inert dead code left in `cli.py`**: the pet rendering subsystem (~250 lines: `_pet_*` methods) is unreachable (command def + config removed, lazy import fails closed) — remove it during the cleanup pass.
- **No live LLM end-to-end run yet**: physics/research modes verified by imports + unit checks + the checker pass/fail cases, but not against a real llama-swap/DeepSeek endpoint.
- **Physics runs are synchronous turns** — a session blocks until the run finishes; the priority queue (deferred in Q13) is the eventual fix.
- **Research pipeline prompts are theory-era text** — experimental-language prompt tuning is a follow-up.
- **TUI** (`ui-tui/src/theme.ts`) still has its own hex color-mix engine — converting it to terminal-theme-driven is a bigger JS refactor, not started.
- **TUI-side `:q`** not verified (composer handles `/` commands locally in `app.tsx`).
- **MessageProvider copy**: setup prompt now mentions llama-swap/DeepSeek ✓; check the `/model` picker/provider-catalog strings for any remaining Nous/OpenRouter-era copy.

## What's left (Phase 7)

1. **AGENTS.md rewrite** — still the hermes dev guide with stale architecture notes (desktop/dashboard/kanban/pets references). Rewrite for the fork.
2. **Minimal test suite as pre-commit** — prune `tests/` to a small passing set (many remaining test files reference removed features and would fail); wire ruff + pytest subset into a pre-commit hook. No GitHub Actions (per Q10).
3. **.env.example + cli-config.yaml.example** — prune removed env vars/sections (`.env.example` was deliberately left untouched so far).
4. **TUI `:q`** and TUI theming decision.
5. **Live smoke test** of the physics modes against the user's endpoints (needs their config).
6. **README** already rewritten (provenance, modes, Nix usage) — keep it in sync with the above.

## Operational notes

- Commits: author `son-of-anton-bot <307402699+son-of-anton-bot@users.noreply.github.com>`, trailer `Co-authored-by: Ethan Todd <30243637+ewtodd@users.noreply.github.com>` (repo git config already set)
- Remote: `git@github.com:ewtodd/son-of-anton.git`, branch `main`
- Verify: `nix flake check` (package + modules + venv import sweep), full-tree compile via `/tmp/opencode/compile_all.py` (path points at `/home/e-play/Software/son-of-anton`), import sweep via `/tmp/opencode/import_sweep.py` (run inside the sealed venv)
- Python for ad-hoc checks: `/nix/store/sgr5qv39ji4gddv37jw1iw069gqxa0x2-python3-3.12.14/bin/python3.12` (bare) or the sealed venv's `bin/python3` (has deps)
- uv (for lock regen): `nix shell nixpkgs#uv -c env UV_PYTHON=/nix/store/sgr5qv39ji4gddv37jw1iw069gqxa0x2-python3-3.12.14/bin/python3.12 uv lock`
- The agent name: the user renamed the GitHub account to `son-of-anton-bot`; do not create a DeepSeek co-author trailer (no such account exists)
