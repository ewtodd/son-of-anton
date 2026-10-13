# Son of Anton — Status

_Last updated 2026-10-12. History lives in git; this file is only current
state, known future work, and the operational facts a fresh session needs._

## What this is

A hard fork of Nous Research's hermes-agent v0.20.5, stripped to an always-on
daemon, with the Autophysicist physics mode ported from
huggingface/physics-intern. One chat loop (`standard`); physics runs only via
`son-of-anton problem create/run`. Textual TUI plus the messaging gateway. MIT.

## Current state

All of the following is merged to `main` and deployed on e-desktop:

- **Dead-code sweep (2026-10-08).** All provider-era residue is gone: the
  OpenAI/Codex OAuth surface, the models.dev no-op stub layer, vendor
  catalogs/branches, 639 unreferenced top-level symbols, and 184 class
  methods. The AST analyzers (top-level + method-aware, with `__all__`,
  string-reference, decorator, framework-dispatch, and plugin-contract
  guards) converge at zero dead symbols; `ruff F821`/`F841` are clean.
- **Reasoning picker.** Bare `/reasoning` opens a picker (Textual modal in
  the TUI, curses radiolist on the plain CLI) limited to the levels the
  active route accepts: the declared
  `custom_providers.<name>.models.<model>.reasoning_efforts` set first, then
  known wire hosts (Kimi, TokenHub), else the widest OpenAI-compatible
  vocabulary. `/reasoning status` prints the old state summary and direct
  `/reasoning <level> [--global]` still works for scripting.
- **Custom-provider catalogs pin the picker.** A `custom_providers` (or the
  keyed `providers:`) entry that declares `models` shows exactly that subset
  in `/model`; `/model --all` widens the row to the endpoint's live catalog
  for one open. Catalogs Son of Anton wrote itself never pin: probe results
  carry `models_discovered`, and wizard saves set `discover_models: true`.
- **Memory scopes.** `MEMORY.md`/`USER.md` are shared; `*.cli.md` and
  `*.gateway.md` are per-surface. The active scope follows the surface
  (`memory.scope` pins it), and the `memory` tool takes a `scope`.
- **Sessions.** Rows record cwd + git root/branch. Bare `-c` resumes the
  current workspace only — no breadcrumb, no global fallback. Bare
  `son-of-anton "prompt"` routes to `chat -q`. `sessions list --json --here`
  is the relay's session picker. One session can be driven from two surfaces
  (the TUI and Signal on a `single_user` instance): the agent records
  `SessionDB.get_transcript_mark()` after each turn (the CLI also at each
  load), and when another process wrote to the session in between, the next
  turn reloads the transcript instead of running on stale in-memory history.
  No foreign write means no reload, so the prompt cache is untouched.
- **Journal.** Persisted rows are mirrored to `journals/<session>.jsonl` with
  the reasoning and tool detail the transcript clips. Never read back
  directly (RAG embeds it); deleted when the user deletes its session, kept
  through retention auto-prune and other automatic cleanup.
- **Relay coding.** The `relay-coding` skill has a chat agent list sessions,
  ask continue-or-new, and run a headless CLI coder in the background with
  `SON_OF_ANTON_DURABLE_APPROVALS=1`. Dangerous commands stage a durable
  approval; a gateway watcher messages the originating chat, and
  `/approve <id>` / `/deny <id>` unblocks the coder. Timeouts fail closed.
- **RAG (optional).** `memory.rag` embeds journals and
  `notes/<scope>/**/*.md` through an OpenAI-compatible `/v1/embeddings`
  endpoint (Bifrost → bge-m3 on oracle) and injects the closest chunks into
  each turn's API copy only. The index refreshes itself every
  `reindex_interval_minutes` (gateway housekeeping loop + interactive CLI
  start); `son-of-anton rag index` forces it. Index format 2: the manifest is
  the commit point (per-file commits, crash-safe, flock-serialized), records
  model + vector width, and journal chunks carry their session's memory scope
  and messaging chat — a chat recalls only itself, the CLI never recalls a
  chat, unidentifiable sessions are not indexed (a row gone before it was
  ever resolved is recorded as an orphan and not re-read until it changes).
  A journal's resolved identity is kept in the manifest, so it stays indexed
  after auto-prune removes its row. Deleting a session deletes its journal
  and its chunks. A format-1 index is unretrievable until the
  first sync rebuilds it.
- **Gateway cache.** Edits to `model.context_length`, `compaction.*`, or a
  per-model `custom_providers` context window rebuild the cached agent on the
  next message instead of waiting for an eviction.
- **Streams.** A terminal usage chunk proves completion, so a provider that
  drops the `finish_reason` chunk no longer triggers a duplicate "continue"
  turn.
- **TOML-only, and the physics package renamed.** Every user-authored format is
  TOML: `config.toml`, `cli-config.toml`, managed config, `skins/*.toml`,
  `problems/*/problem.toml`, `plugin.toml` manifests, SKILL.md frontmatter
  (still `---` fenced, TOML body), `locales/*.toml`, and status phrases.
  Reading is stdlib `tomllib`; writing goes through the serializer in
  `utils.py` (no comment preservation — a TOML edit rewrites the document).
  The physics mode lives in `autophysicist/` (was `physics_intern/`), with the
  former `physics_intern/autophysicist/` loop flattened to the package root.
  The `pyyaml`/`ruamel.yaml` pins are gone from `pyproject.toml` and the
  YAML write linter in `tools/file_operations.py` is removed, so first-party
  code imports no YAML library. `uv.lock` still carries `pyyaml` as a
  transitive dependency of `uvicorn[standard]`.
- **Third-party YAML is only recognized, never parsed.** LSP/media detection
  still recognize `.yaml`/`.yml` for external projects, but nothing in Son of
  Anton parses them.
- **Human critic mode.** `physics.critic.mode = "human"` for attended runs:
  each critique renders the iteration's activity and `git diff HEAD`, opens
  `$EDITOR`, and takes free-text feedback plus a structured verdict
  (`progress | stalled | wrong_approach | needs_help`) and a block-next flag.
  The text is injected through exactly the same channel as a model critique,
  so the Manager cannot tell them apart. Every critique — model or human —
  lands in `CRITIQUE_DATA.jsonl` (source, verdict, block flag, diff/text
  hashes) and its provenance appears in the `CRITIQUE_LOG.md` header. No
  editor and no TTY means the critique is skipped, the same fail-open contract
  as a failed model critic.

## Known future work

Ordered by importance. Each item is independently scoped.

1. **Delegation and sub-agents are broken.** _Resolved._ Root-caused from the
   2026-10-07 logs and fixed end to end (E2E against `unholy/dsv4-flash`):
   - A child whose API calls all failed was reported `completed` /
     `max_iterations` / `truncated` with the error text as its summary. One
     mapping (`_classify_child_result`) now makes `failed`/`partial` turns
     `failed` with the error in `error`, on every sync/batch/async path.
   - The output-schema retry ran with no history (the child redid the whole
     task from "your previous response was rejected"), also after failed turns,
     and outside the subagent harness. It now continues the child's own
     transcript, only after a turn that produced an answer, through the same
     worker/approval/timeout harness as the first turn (`_run_child_turn`).
   - Background results were acknowledged in state.db when queued in memory,
     and since the Textual port only the user's next message drained that
     queue — results waited hours, and one refused by a lease-abort turn was
     lost. Delivery is acknowledged only once a turn took the result (refused
     turns re-queue it; leftovers are released at shutdown), and the TUI's idle
     tick runs queued backend work (which also restores `/loop` wakeups).
   - Left as is, deliberately: the reasoning-model stale floor (e.g. `qwen3`
     → 180s) still applies to local endpoints, so a slow local reasoning model
     as the delegation model needs `stale_timeout_seconds` on its provider;
     the default `unholy/dsv4-flash` matches no floor. An interrupt from the
     previous turn can still abort the next turn's lease wait (results now
     survive it). Tests in `tests/test_delegation_outcomes.py` and
     `tests/test_delegation_delivery.py`.
2. **Live-validate the relay round trip.** The code is complete and
   unit-tested, but no real Signal coding request has gone through the whole
   path: skill → background coder → dangerous command → watcher prompt in the
   chat → `/approve` → coder resumes → completion summary. Do this before
   building anything on top of the relay.
3. **One coder per workspace is not enforced.** Only the `relay-coding`
   skill's instruction prevents two concurrent coders in the same directory.
   If that becomes a real failure mode, add a workspace-keyed lease.
4. **`session_search` is not scope-filtered.** _Resolved._ The tool gained a
   `sources` filter (comma-separated, e.g. `sources="cli"`) threaded through
   the browse, discovery, and title-match paths into
   `list_sessions_rich(sources=)` / `search_messages(source_filter=)`. Omitting
   it is the explicit "search everything" escape. The state layer already
   supported both filters, so this was surfacing, not new query machinery.
   Tests in `tests/test_session_search_sources.py`.
5. **RAG search is a pure-Python cosine scan.** Fine for hundreds of chunks.
   If the journals grow into the tens of thousands, add a keyword/FTS
   prefilter or a candidate cap before ranking.
6. **RAG refresh should self-register.** _Resolved, without cron._ The
   gateway housekeeping loop calls `agent.rag.maybe_refresh_index_async()`
   every tick; it is gated by `memory.rag.enabled` and
   `memory.rag.reindex_interval_minutes` (default 15, `0` off) against a
   cross-process `.last_sync` marker, and syncs on a daemon thread under a
   non-blocking lock, so overlapping runs skip. The TUI does the same once at
   start for CLI-only installs. Tests in `tests/test_rag.py`.
7. **Cron jobs must always run the current default model.** _Resolved (opt-out)._
   The model is already re-resolved from config on every tick; what blocked
   changing it was the fail-closed #44585 drift guard, which skips an *unpinned*
   job when the global default moves (real overage, so the default stays on).
   The intended escape — `cron.model_drift_guard: false` — is now documented in
   the README ("Cron") and covered by `tests/test_cron_model_drift_guard.py`;
   this instance has it off (local models, no spend), so unpinned jobs track the
   live default.
8. **Deep-Nous prose residue.** _Resolved._ The relay identity-token and
   self-provision docstrings describe the two remaining modes (operator IdP,
   ambient endpoint), and self-provision skips cleanly when neither is
   configured instead of posting an empty token. The gateway's proxy mode
   (`GATEWAY_PROXY_URL`, a forwarder to the deleted api_server) is removed.
9. **TUI gaps from the REPL.** _Resolved._ The Textual TUI carries `/image`
   and `/paste` attachments plus `/agents`
   (`son_of_anton_cli/commands.py`, `cli_commands_mixin.py`).
10. **Physics sub-agents vanish when they exhaust their round cap.**
    _Resolved._ In the YAP alpha/gamma calibration run
    (`workspace-soa/runs/20261002_153415_*`) 7 of 9 dispatches came back empty:
    a sub-agent with lookup tools spent all 6 rounds on lookups,
    `run_agent_loop` returned `text=""` on the round cap, and
    `dispatch_subagent` reported `no_code`, which the Manager misread as a
    prompting problem. Now sub-agents run with
    `final_answer_on_max_rounds=True` — on the cap they get one tools-off
    request for their answer, falling back to their last non-empty message —
    and a truncated final answer is continued the way `call_llm` continues one.
    `dispatch_subagent` reports why an answer is unusable
    (`execution_status` `max_rounds` / `truncated` / `empty_response`) and
    `format_for_manager` shows that status (and "No Python code block found",
    which it used to drop). Tests in `tests/test_physics_truncation.py`. The
    round cap stays at 6. Secondary run issues from that run, still open:
    0 durable output across 2 iterations (no RESULTS.txt / features / calib),
    critic is ~298s per iteration, `df_cache` capped at 50k events vs
    1.8M–13.4M real, and waveform polarity (+1 vs −1) never settled by an
    artifact. (The "two `Data_R` trees" in each YAP file are ROOT AutoSave
    cycles of one tree — e.g. `DataR_Am241.root` holds `Data_R;18` with
    1,830,886 entries and the older snapshot `;17` with 1,759,126 — so reading
    the newest cycle is correct, not a bug.)

## Operational notes

- Repo `git@github.com:ewtodd/son-of-anton.git`, branch `main`. Sole
  authorship, no `Co-authored-by` trailers; the bot commit identity is in the
  repo git config.
- Tests: `nix develop -c scripts/run_tests.sh` (1005 tests, 87 files, ~50s).
  Pre-commit (`nix develop -c pre-commit install`) runs ruff plus that suite.
- Deployment: `/etc/nixos` host `e-desktop`, flake input `son-of-anton`
  following `main`; bump the input and reactivate. Bifrost on oracle fronts
  vLLM, gufo, llama-swap, DeepSeek, and bge-m3 embeddings; clients use the
  `BIFROST_SOA_VK` virtual key.
- Notes for RAG go in `~/.son-of-anton/notes/<shared|cli|gateway>/` — dropping
  a markdown file there is the whole registration step.
- Upstream refs: hermes-agent v0.20.5 (`fcbd1076a9`), physics-intern
  (`5553bb6`).
