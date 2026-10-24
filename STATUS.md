# Son of Anton — Status

_Last updated 2026-10-17. History lives in git; this file is only current
state, open work, and the operational facts a fresh session needs._

## What this is

A hard fork of Nous Research's hermes-agent v0.20.5, cut down to what the
owner's deployments use, with the Autophysicist physics mode ported from
huggingface/physics-intern. One chat loop; physics runs only via
`son-of-anton problem create/run`. Textual TUI plus a Signal gateway. Linux and
Nix only. MIT.

## Current state

- **Deployed:** e-desktop's `/etc/nixos` pins `fe9d6755` (2026-10-16).
- **On `main`, not yet deployed:** the purge of 2026-10-16/17. Removed:
  - Discord, Slack and the Nous relay connector;
  - every model provider but `custom`;
  - memory-provider plugins, every web backend but SearXNG, and the plugin
    system itself;
  - image/video generation, TTS, browser automation, Microsoft Graph, the
    dashboard and telemetry;
  - Windows/macOS/non-Nix install paths, and the setup wizard.

  Python went from 431,619 to ~240,000 lines and `uv.lock` from 127 to 70
  packages. For the owner's config, tool schemas, system prompts and model
  request bodies are byte-identical to `fe9d6755`.
- **What runs:**
  - one OpenAI-compatible provider (`custom_providers`, maitred);
  - the TUI and single-query CLI;
  - the Signal gateway for single_user instances (work, play) and shared
    groups (house, ricky, markets);
  - the relay (`relay` tool, `/handoff`, `/pickup`);
  - durable approvals;
  - MCP (HTTP with Bearer headers, stdio via socat) and `mcp serve`;
  - built-in memory with scopes, plus RAG over journals and notes;
  - cron, delegation and `execute_code`;
  - physics.

## Open work

1. **Deploy the purge and validate on real Signal.** Run `nix flake update
   son-of-anton` in /etc/nixos, commit `flake.lock`, then `nrs`. Check one relay
   round trip with `/approve` from the phone, `/handoff`, and a `--pickup` of
   a running worker.
2. **`web_extract` goes through a page-extraction service.** It posts each URL
   to `WEB_EXTRACT_URL` (the Crawl4AI service on mu, `/etc/nixos`
   `modules/services/web-extract`) and is offered only when that is set. Open:
   sites that wall off headless browsers (eBay search) answer with an error.
3. **The relay is advisory.** A single_user chat keeps its own terminal, file
   and code tools; only the relay tool's description keeps project work out of
   the chat. On 2026-10-16 a four-hour Remix-MUSIC-Sim build ran entirely in
   the chat. Plan: no file/terminal/code tools for single_user chats. A relay
   for shared groups needs its own design: who may drive workers and approve.
4. **v's laptop has no web search.** It used the removed keyless ring.
   maitred serves SearXNG over `/mcp`, but granting it to the shared
   son-of-anton key would duplicate the tools on e-desktop.
5. **One coder per workspace is not enforced.** Nothing keeps two workers, or
   a worker and a TUI, out of the same directory. Needs a workspace-keyed
   lease.
6. **RAG search is a pure-Python cosine scan.** Fine for hundreds of chunks;
   add a prefilter or candidate cap if journals reach tens of thousands.
7. **Physics run issues from the YAP calibration run**
   (`workspace-soa/runs/20261002_153415_*`):
   - no durable output across 2 iterations;
   - about 298 s of critic time per iteration;
   - `df_cache` capped at 50k events;
   - waveform polarity never settled by an artifact.

## Operational notes

- **Repo:** `git@github.com:ewtodd/son-of-anton.git`, branch `main`. Sole
  authorship, no `Co-authored-by` trailers; the bot identity is in the repo
  git config.
- **Gates:** `nix develop -c bash -c 'ruff check . && scripts/run_tests.sh'`
  (about 1,140 tests, under two minutes) and `nix flake check`. There is no CI
  and no pre-commit hook.
- **Infrastructure:**
  - maitred on mu (10.0.0.2:4002, `/v1` and `/mcp`, key `BIFROST_SOA_VK`)
    fronts the models and the bge-m3 embeddings;
  - signal-cli also runs on mu (`SIGNAL_HTTP_URL`);
  - SearXNG is on mu, port 8888.
- **RAG notes** go in `~/.son-of-anton/notes/<shared|cli|gateway>/`; dropping a
  markdown file there is the whole registration step.
- **Upstream refs:** hermes-agent v0.20.5 (`fcbd1076a9`), physics-intern
  (`5553bb6`).
