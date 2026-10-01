# Son of Anton — Status

_Last updated 2026-10-01. History lives in git; this file is only current
state, known future work, and the operational facts a fresh session needs._

## What this is

A hard fork of Nous Research's hermes-agent v0.20.5, stripped to an always-on
daemon, with the Autophysicist physics mode ported from
huggingface/physics-intern. One chat loop (`standard`); physics runs only via
`son-of-anton problem create/run`. Textual TUI plus the messaging gateway. MIT.

## Current state

All of the following is merged to `main` and deployed on e-desktop:

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

## Known future work

Ordered by importance. Each item is independently scoped.

1. **Live-validate the relay round trip.** The code is complete and
   unit-tested, but no real Signal coding request has gone through the whole
   path: skill → background coder → dangerous command → watcher prompt in the
   chat → `/approve` → coder resumes → completion summary. Do this before
   building anything on top of the relay.
2. **One coder per workspace is not enforced.** Only the `relay-coding`
   skill's instruction prevents two concurrent coders in the same directory.
   If that becomes a real failure mode, add a workspace-keyed lease.
3. **`session_search` is not scope-filtered.** Memory is scoped; session
   search still returns every surface's sessions. Add the active-scope filter
   with an explicit "search everything" escape.
4. **RAG search is a pure-Python cosine scan.** Fine for hundreds of chunks.
   If the journals grow into the tens of thousands, add a keyword/FTS
   prefilter or a candidate cap before ranking.
5. **RAG refresh should self-register.** When `memory.rag.enabled` is set, a
   `rag index` cron job should be created automatically (idempotently, per
   instance) instead of requiring `son-of-anton cron create` by hand. Until
   then, run `son-of-anton rag index` manually or cron it yourself.
6. **Cron jobs must always run the current default model.** A job currently
   snapshots model/provider at creation and drift-checks it later, so changing
   the default model makes jobs fail (`drift_skip`) or run a stale model.
   Unless a job explicitly pins a model, each run should resolve the live
   configured default.
7. **Deep-Nous prose residue.** The gateway relay's enroll docstring still
   names `resolve_nous_access_token()` (never called), and `doctor.py` carries
   an inert removed-provider probe. Cosmetic.
8. **TUI gaps from the REPL.** Prompt image attachments and an `/agents`
   viewer were never carried over.

## Operational notes

- Repo `git@github.com:ewtodd/son-of-anton.git`, branch `main`. Sole
  authorship, no `Co-authored-by` trailers; the bot commit identity is in the
  repo git config.
- Tests: `nix develop -c scripts/run_tests.sh` (712 tests, 65 files, ~35s).
  Pre-commit (`nix develop -c pre-commit install`) runs ruff plus that suite.
- Deployment: `/etc/nixos` host `e-desktop`, flake input `son-of-anton`
  following `main`; bump the input and reactivate. Bifrost on oracle fronts
  vLLM, gufo, llama-swap, DeepSeek, and bge-m3 embeddings; clients use the
  `BIFROST_SOA_VK` virtual key.
- Notes for RAG go in `~/.son-of-anton/notes/<shared|cli|gateway>/` — dropping
  a markdown file there is the whole registration step.
- Upstream refs: hermes-agent v0.20.5 (`fcbd1076a9`), physics-intern
  (`5553bb6`).
