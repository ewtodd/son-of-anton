# Son of Anton — Status

_Last updated 2026-10-08. History lives in git; this file is only current
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
  is the relay's session picker.
- **Journal.** Persisted rows are mirrored to `journals/<session>.jsonl` with
  the reasoning and tool detail the transcript clips. Write-only; never
  injected.
- **Relay coding.** The `relay-coding` skill has a chat agent list sessions,
  ask continue-or-new, and run a headless CLI coder in the background with
  `SON_OF_ANTON_DURABLE_APPROVALS=1`. Dangerous commands stage a durable
  approval; a gateway watcher messages the originating chat, and
  `/approve <id>` / `/deny <id>` unblocks the coder. Timeouts fail closed.
- **RAG (optional).** `memory.rag` embeds journals and
  `notes/<scope>/**/*.md` through an OpenAI-compatible `/v1/embeddings`
  endpoint (Bifrost → bge-m3 on oracle) and injects the closest chunks into
  each turn's API copy only. `son-of-anton rag index` refreshes it
  incrementally.
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

1. **Delegation and sub-agents are broken.** `delegate_task` does not
   reliably run or return usable results (reported 2026-10-08; not yet
   root-caused). Debug the dispatch/execution path end to end before building
   anything on top of it. The physics sub-agent round-cap failure (item 10)
   is a separate, diagnosed instance of sub-agents coming back empty.
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
6. **RAG refresh should self-register.** When `memory.rag.enabled` is set, a
   `rag index` cron job should be created automatically (idempotently, per
   instance) instead of requiring `son-of-anton cron create` by hand. Until
   then, run `son-of-anton rag index` manually or cron it yourself.
7. **Cron jobs must always run the current default model.** _Resolved (opt-out)._
   The model is already re-resolved from config on every tick; what blocked
   changing it was the fail-closed #44585 drift guard, which skips an *unpinned*
   job when the global default moves (real overage, so the default stays on).
   The intended escape — `cron.model_drift_guard: false` — is now documented in
   the README ("Cron") and covered by `tests/test_cron_model_drift_guard.py`;
   this instance has it off (local models, no spend), so unpinned jobs track the
   live default.
8. **Deep-Nous prose residue.** The gateway relay's enroll docstring still
   names `resolve_nous_access_token()` (never called;
   `gateway/relay/__init__.py:528/633/651`). Cosmetic. (The old `doctor.py`
   probe is gone — the file was deleted in `bebf5cd6`.)
9. **TUI gaps from the REPL.** _Resolved._ The Textual TUI carries `/image`
   and `/paste` attachments plus `/agents`
   (`son_of_anton_cli/commands.py`, `cli_commands_mixin.py`).
10. **Physics sub-agents vanish when they exhaust their round cap.** In the
    YAP alpha/gamma calibration run (`workspace-soa/runs/20261002_153415_*`),
    7 of 9 dispatches came back empty. Root cause: a sub-agent with lookup
    tools runs through `run_agent_loop(..., max_rounds=6)`
    (`autophysicist/subagent.py:210`); when it spends all 6 rounds calling
    context7 / analysis_utilities lookups and never emits a final plain-text
    answer, `run_agent_loop` (`llm.py:562`) returns `text=""` on the
    `max_rounds` fallback (the normal exit path returns `message.content`).
    `stop_reason="max_rounds"` **is** propagated in the `AgentResult`
    (`llm.py:566`), but `dispatch_subagent` ignores it and reports
    `execution_status="no_code"`, so the Manager sees "the model wrote no
    code" and misdiagnoses it as a prompting problem (it spent iteration 2
    "confirming" a dispatch rule that was never the cause). Every *substantive*
    task that needs several doc lookups before writing a script dies this way;
    only trivial ≤1-lookup tasks survive. Fix: in `run_agent_loop`, track the
    last non-empty `message.content` and return it on the `max_rounds`
    fallback; teach `dispatch_subagent` to read `stop_reason` and set a
    distinct `execution_status="max_rounds"` so the Manager re-dispatches
    tighter (or with more rounds) instead of "it returned nothing". Optionally
    raise `max_rounds=6` → 8–10. Secondary run issues noted for follow-up:
    0 durable output across 2 iterations (no RESULTS.txt / features / calib),
    critic is ~298s per iteration, `df_cache` capped at 50k events vs
    1.8M–13.4M real, waveform polarity (+1 vs −1) never settled by an
    artifact, and pure files carry two `Data_R` trees while `load_tree_data`
    reads only the first.

## Operational notes

- Repo `git@github.com:ewtodd/son-of-anton.git`, branch `main`. Sole
  authorship, no `Co-authored-by` trailers; the bot commit identity is in the
  repo git config.
- Tests: `nix develop -c scripts/run_tests.sh` (742 tests, 68 files, ~35s).
  Pre-commit (`nix develop -c pre-commit install`) runs ruff plus that suite.
- Deployment: `/etc/nixos` host `e-desktop`, flake input `son-of-anton`
  following `main`; bump the input and reactivate. Bifrost on oracle fronts
  vLLM, gufo, llama-swap, DeepSeek, and bge-m3 embeddings; clients use the
  `BIFROST_SOA_VK` virtual key.
- Notes for RAG go in `~/.son-of-anton/notes/<shared|cli|gateway>/` — dropping
  a markdown file there is the whole registration step.
- Upstream refs: hermes-agent v0.20.5 (`fcbd1076a9`), physics-intern
  (`5553bb6`).
