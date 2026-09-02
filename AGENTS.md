# Son of Anton Agent - Development Guide

Instructions for AI coding assistants and developers working on the son-of-anton codebase.

**Never give up on the right solution.**

## What Son of Anton Is

Son of Anton is a hard fork of [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (upstream commit `fcbd1076a9`), stripped to a lean always-on daemon
surface, and extended with the physics modes from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`). MIT licensed. It is the successor to the archived
[temple](https://github.com/ewtodd/temple) harness — the daemon design,
permission modes, and request router carry over from it.

Three agent modes, selected per request by a heuristic router with a `/mode` override:

- `standard` — the hermes agent loop (terminal, files, web, skills, memory, delegation, cron)
- `physics` — Autophysicist: a single research manager with permanent memory + scratchpad,
  token budget, `submit_final_answer`, git workspace
- `research` — a nine-agent critical self-research pipeline (surveyor → planner →
  orchestrator → researcher/computer → reviewer → critic → adjudicator → formatter)

Two properties shape almost every design decision and are the lens for reviewing any change:

- **Per-conversation prompt caching is sacred.** A long-lived conversation reuses a cached
  prefix every turn. Anything that mutates past context, swaps toolsets, or rebuilds the
  system prompt mid-conversation invalidates that cache and multiplies the user's cost.
  We do not do it (the one exception is context compression).
- **The core is a narrow waist; capability lives at the edges.** Every model tool we add
  is sent on every API call, so the bar for a new *core* tool is high. Most new capability
  should arrive as a CLI command + skill, a plugin, or an MCP server — not as core surface.

## Contribution Rubric — What We Want / What We Don't

- **Fix real bugs, well.** A good fix reproduces the symptom on current `main`, points to
  the exact line where it manifests, and fixes the whole bug class — sibling call paths
  included — not just the one site the reporter hit.
- **Keep the core narrow.** Prefer, in order: extend existing code → CLI command + skill →
  service-gated tool → plugin → MCP server in the catalog → new core tool (last resort).
  See "The Footprint Ladder" below.
- **Extend, don't duplicate.** Before adding a module/manager/hook, check whether existing
  infrastructure already covers the use case.
- **Behavior contracts over snapshots.** Tests should assert how two pieces of data must
  relate (invariants), not freeze a current value. See "Don't write change-detector tests."
- **E2E validation, not just green unit mocks.** For anything touching resolution chains,
  config propagation, security boundaries, remote backends, or file/network I/O, exercise
  the real path with real imports against a temp `SON_OF_ANTON_HOME`.
- **Cache-, alternation-, and invariant-safe.** Preserve prompt caching, strict message
  role alternation (never two same-role messages in a row; never a synthetic user message
  injected mid-loop), and a system prompt that is byte-stable for the life of a conversation.

### Before you call it a bug — verify the premise

- **"Intentional design, not a gap."** A limitation that looks like an oversight is often
  deliberate. Read the original commit's intent (`git log -p -S "<symbol>"`) before assuming
  something is unfinished.
- **"The premise doesn't hold against how X actually works."** Trace the real code/runtime
  before accepting a rationale. If you can't point to the exact line where the bug manifests
  AND show the fix changes that line's behavior, you haven't verified the premise.
- **"This fix was wrong — the absence/omission was deliberate."** Adding the obvious-looking
  missing piece can break things the omission was protecting.
- **"Overreached."** Scope creep beyond the agreed change gets rejected even when the code
  works. Keep the change to the narrow piece that was actually agreed.

The throughline: **verify the claim AND the intent against the codebase before writing or
merging a fix.** When in doubt about intent, it is cheaper to ask than to ship a fix that
fights the design.

### The Footprint Ladder (new capability decision)

Each rung adds more permanent surface than the one above. Choose the highest
(least-footprint) rung that correctly solves the problem:

1. **Extend existing code** — the capability is a variation of something that already
   exists. Zero new surface.
2. **CLI command + skill** — manages config/state/infra expressible as shell commands.
   Zero model-tool footprint. Default choice for subscriptions, scheduled tasks, service
   setup. Examples: `son-of-anton cron`, `son-of-anton tools`.
3. **Service-gated tool (`check_fn`)** — needs structured params/returns AND only appears
   when a prerequisite is configured. Zero footprint otherwise.
4. **Plugin** — user/niche capability that doesn't ship in core. Lives in
   `~/.son-of-anton/plugins/` or a pip package, discovered at runtime.
5. **MCP server (in the catalog)** — if the capability genuinely needs to be a tool
   (structured I/O the agent invokes) but isn't core-fundamental, prefer building it as an
   MCP server over growing the core toolset. The built-in MCP client connects to it; zero
   permanent core-schema footprint.
6. **New core tool** — only when the capability is fundamental, broadly useful to nearly
   every user, and unreachable via terminal + file (or an MCP server). Examples of correct
   core tools: terminal, read_file, web_search.

## Development Environment

This fork is Nix-first. Python dependencies are sealed in a uv2nix venv; the `son-of-anton`
wrapper exposes the bundled skills and plugins via `SON_OF_ANTON_BUNDLED_SKILLS` /
`SON_OF_ANTON_BUNDLED_PLUGINS`.

```bash
nix build                # sealed uv2nix venv + wrapper in result/bin/
nix flake check          # package + modules + venv import sweep
nix run .# --            # start the CLI
nix develop              # python dev shell with the editable venv
```

Ad-hoc Python checks against the working tree can run in the sealed venv:

```bash
/nix/store/<hash>-son-of-anton-env/bin/python3 -c "import sys; sys.path.insert(0, '.'); ..."
```

**Always use `scripts/run_tests.sh`** for the Python test suite — never bare `pytest`.
The script enforces hermetic environment parity (unset credential vars, TZ=UTC, LANG=C.UTF-8,
per-file subprocess isolation) and runs through the Nix dev shell's sealed
environment (`nix develop` sets `SON_OF_ANTON_PYTHON`); there is no pip/venv fallback.

For uv lock regeneration:

```bash
nix shell nixpkgs#uv -c env UV_PYTHON=<nix-python-3.12> uv lock
```

### Commit convention

- Author: `son-of-anton-bot <307402699+son-of-anton-bot@users.noreply.github.com>`
  (repo git config already set — do not change it)
- Sole authorship. No `Co-authored-by:` trailers, for anyone — not the repo owner,
  not the model, not an upstream account.
- Remote: `git@github.com:ewtodd/son-of-anton.git`, branch `main`
- Before merging anything, make sure the branch is up to date with `main` — a stale
  branch's version of an unrelated file silently overwrites recent fixes on squash-merge.

## Project Structure

File counts shift constantly — don't treat the tree below as exhaustive. The canonical
source is the filesystem.

```
son-of-anton/
├── run_agent.py          # AIAgent class — core conversation loop
├── model_tools.py        # Tool orchestration, discover_builtin_tools(), handle_function_call()
├── toolsets.py           # Toolset definitions, _SON_OF_ANTON_CORE_TOOLS list
├── cli.py                # SonOfAntonCLI class — interactive CLI orchestrator
├── son_of_anton_state.py       # SessionDB — SQLite session store (FTS5 search)
├── son_of_anton_constants.py   # get_son_of_anton_home(), display_son_of_anton_home()
├── son_of_anton_logging.py     # setup_logging() — agent.log / errors.log / gateway.log
├── agent/                # Agent internals (providers, memory, caching, compression, ...)
├── son_of_anton_cli/           # CLI subcommands, setup wizard, plugins loader, skin engine
│   └── router.py         # classify_mode() / resolve_mode() — the three-mode router
├── tools/                # Tool implementations — auto-discovered via tools/registry.py
│   └── environments/     # Terminal backends (local, ssh)
├── gateway/              # Messaging gateway — run.py + session.py + platforms/
│   └── platforms/        # signal (built-in); discord + slack live in plugins/platforms/
├── plugins/              # Plugin system (see "Plugins" below)
│   ├── memory/           # Memory-provider plugins (honcho, mem0, supermemory, ...)
│   ├── model-providers/  # Inference backend plugins (custom)
│   ├── platforms/        # discord, slack adapters
│   └── web/              # Web-search provider plugins (exa, tavily, firecrawl, ...)
├── physics_intern/       # Vendored physics modes (Autophysicist + research pipeline)
│   ├── llm.py            # OpenAI-compatible layer resolving endpoints from config.yaml
│   └── verification/     # Experimental verification (RESULTS.txt, checker scripts)
├── problems/             # Toy physics problems (cobalt_calibration, bromine_halflife, ...)
├── cron/                 # Scheduler — jobs.py, scheduler.py
├── skills/               # Bundled skills (skills/<category>/<skill>/SKILL.md)
├── scripts/              # run_tests.sh, release.py, auxiliary scripts
└── tests/                # Pytest suite (small, focused set — see "Testing")
```

**User config:** `~/.son-of-anton/config.yaml` (settings), `~/.son-of-anton/.env` (secrets only).
**Logs:** `~/.son-of-anton/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+),
`gateway.log` when running the gateway. Profile-aware via `get_son_of_anton_home()`.
Browse with `son-of-anton logs [--follow] [--level ...] [--session ...]`.

### File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, environments/
```

---

## AIAgent Class (run_agent.py)

The real `AIAgent.__init__` takes many parameters (credentials, routing, callbacks,
session context, budget, credential pool, etc.). Read `run_agent.py` for the full list.

```python
class AIAgent:
    def __init__(self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,              # "chat_completions" | "codex_responses" | ...
        model: str = "",                   # empty → resolved from config/provider later
        max_iterations: int = 500,
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        platform: str = None,              # "cli", "discord", "slack", "signal", ...
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        # ... plus callbacks, thread/user/chat IDs, budgets, fallback_model,
        # checkpoints config, prefill_messages, etc.
    ): ...

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Full interface — returns dict with final_response + messages."""
```

### Agent Loop

The core loop is inside `run_conversation()` — synchronous, with interrupt checks,
budget tracking, and a one-turn grace call:

```python
while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0) \
        or self._budget_grace_call:
    if self._interrupt_requested: break
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Messages follow OpenAI format: `{"role": "system/user/assistant/tool", ...}`.
Reasoning content is stored in `assistant_msg["reasoning"]`.

---

## The Three-Mode Router (son_of_anton_cli/router.py)

`classify_mode(text)` picks `physics` / `research` / `standard` from keyword heuristics,
and `resolve_mode()` applies it to the FIRST message of a session only — the one-shot
physics/research loops get no conversation history, so re-routing a follow-up would
discard the exchange. Config section `router:` holds `enabled` and `modes`.
`/mode auto|standard|physics|research` pins the session mode on any turn.

The router does not pick models. Temple's `classify_complexity()` / model-slot half was
ported here and never wired to anything; it was deleted rather than left to mislead.

Physics keywords ("fit the histogram", "half-life", "cross-section", ...) route to
`physics`; research keywords ("derive the", "literature review", ...) route to `research`;
everything else uses the standard loop.

---

## CLI Architecture (cli.py)

- **Rich** for banner/panels, **prompt_toolkit** for input with autocomplete.
- **KawaiiSpinner** (`agent/display.py`) — animated faces during API calls,
  `┊` activity feed for tool results.
- `load_cli_config()` in cli.py merges hardcoded defaults + user config YAML.
- **Skin engine** (`son_of_anton_cli/skin_engine.py`) — data-driven CLI theming;
  initialized from `display.skin` at startup. Skins are pure data (colors, spinner
  faces/verbs, tool prefix, branding) — built-ins: `default`, `ares`, `mono`, `slate`,
  `daylight`, `warm-lightmode`, `poseidon`, `sisyphus`, `charizard`, plus user skins
  in `~/.son-of-anton/skins/*.yaml`. `/skin <name>` switches live.
- `process_command()` dispatches on the canonical command name resolved via
  `resolve_command()` from the central registry (`son_of_anton_cli/commands.py`).
- Skill slash commands: `agent/skill_commands.py` scans `~/.son-of-anton/skills/` and
  injects as a **user message** (not system prompt) to preserve prompt caching.

### Slash Command Registry (`son_of_anton_cli/commands.py`)

All slash commands are defined in a central `COMMAND_REGISTRY` list of `CommandDef`
objects. Every downstream consumer derives from it automatically: CLI dispatch, gateway
dispatch (`GATEWAY_KNOWN_COMMANDS`), `/help` output, autocomplete, Telegram-style menus.

**CommandDef fields:** `name` (canonical, no slash), `description`, `category`
(`Session` | `Configuration` | `Tools & Skills` | `Info` | `Exit`), `aliases` (tuple),
`args_hint`, `cli_only`, `gateway_only`, `gateway_config_gate` (config dotpath that makes
a `cli_only` command available in the gateway when truthy).

Adding a command = one `CommandDef` entry + a handler in `SonOfAntonCLI.process_command()`
(+ a gateway handler if it should work on messaging platforms). Aliases update help,
menus, and autocomplete automatically.

---

## Interface

The classic prompt_toolkit CLI (`son-of-anton`) is the only interface —
the Ink TUI (`ui-tui/`, `tui_gateway/`) was removed on 2026-08-25. The
Nix package never shipped the esbuild bundle, so `--tui` could only fail
with a bogus "workspace missing" error; the CLI covers everything.

---

## Gateway (Discord + Slack + Signal)

One gateway process runs the platform adapters and the cron scheduler. Discord and Slack
live in `plugins/platforms/`; Signal is built-in (`gateway/platforms/signal.py`).
Read `gateway/platforms/ADDING_A_PLATFORM.md` when adding a platform.

The gateway has TWO message guards — both must bypass approval/control commands. When an
agent is running, messages pass through (1) the base adapter's `_pending_messages` queue
and (2) the runner's interception of `/stop`, `/new`, `/queue`, `/status`, `/approve`,
`/deny`. Any new command that must reach the runner while the agent is blocked MUST bypass
BOTH guards and be dispatched inline.

Cron deliveries are **not** mirrored into the target gateway session — they land in their
own cron session with a header/footer frame so the main conversation's message-role
alternation stays intact.

---

## Physics Modes (physics_intern/)

- `physics_intern/llm.py` resolves endpoints in order: `physics.base_url` (config.yaml) →
  provider defaults (openai → `api.openai.com`) →
  `custom_providers.<provider>.base_url` → `http://127.0.0.1:8080/v1`. Retry +
  context-length detection included.
- Autophysicist is a callable `run_autophysicist(...)`; the research pipeline runs nine
  agents over a structured `ResearchState`.
- **Experimental verification** (`physics_intern/verification/experimental.py`): numeric
  `checks` against a problem spec, workspace `RESULTS.txt` (`key = value`), optional
  checker script, `FORMAL_EVAL.md`. Toy problems live in `problems/` (data synthesized by
  the model — real UM-ANSG data is private).
- Both modes are wired into the CLI `chat()` and gateway turns (ack → worker-thread run →
  ANSWER + eval delivered back). The wheel ships physics data files via package-data.

Physics runs are **synchronous turns** — a session blocks until the run finishes. That is
a known limitation, not a bug.

---

## Adding New Tools

Before adding any tool, settle the footprint question first (see "The Footprint Ladder"):
most capabilities should NOT be core tools. For custom or local-only tools, use the plugin
route: create `~/.son-of-anton/plugins/<name>/plugin.yaml` and
`~/.son-of-anton/plugins/<name>/__init__.py`, then register tools with
`ctx.register_tool(...)`. Plugin toolsets are discovered automatically and can be enabled
or disabled without touching `tools/` or `toolsets.py`.

Built-in/core tools require changes in **2 files**:

**1. Create `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add to `toolsets.py`** — either `_SON_OF_ANTON_CORE_TOOLS` (all platforms) or a new
toolset. **This step is required:** auto-discovery imports the tool and registers its
schema, but the tool is only *exposed to an agent* if its name appears in a toolset.

Auto-discovery: any `tools/*.py` file with a top-level `registry.register()` call is
imported automatically — no manual import list. The registry handles schema collection,
dispatch, availability checking, and error wrapping. All handlers MUST return a JSON string.

**Path references in tool schemas**: use `display_son_of_anton_home()` for user-facing
paths; the schema is generated at import time, after profile overrides apply.
**State files**: use `get_son_of_anton_home()` — never `Path.home() / ".son-of-anton"`.
**Agent-level tools** (todo, memory): intercepted by `run_agent.py` before
`handle_function_call()`.

## Dependency Pinning Policy

All dependencies must have upper bounds to limit supply-chain attack surface.

| Source type | Treatment | Example |
|---|---|---|
| PyPI package | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | Commit SHA | `git+https://...@<40-char-sha>` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

When adding a dependency to `pyproject.toml`: pin `>=current,<next_major` (post-1.0) or
`<0.(minor+2)` (pre-1.0); never a bare `>=X`; then run `uv lock`.

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `son_of_anton_cli/config_defaults.py`.
2. Bump `_config_version` ONLY for migrations that transform existing user config.
   Adding a new key to an existing section is handled by the deep-merge — no bump needed.

### .env variables (SECRETS ONLY — API keys, tokens, passwords):
1. Add to `OPTIONAL_ENV_VARS` in `son_of_anton_cli/config_defaults.py` with metadata
   (`description`, `prompt`, `url`, `password`, `category`).

Non-secret settings (timeouts, thresholds, feature flags, paths, display prefs) belong in
`config.yaml`, not `.env`. If internal code needs an env var mirror, bridge it from
config.yaml in code.

### Config loaders (three paths — know which one you're in):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` — merges CLI-specific defaults + user YAML |
| `load_config()` | `son-of-anton tools`, `son-of-anton setup`, most subcommands | `son_of_anton_cli/config.py` — merges `DEFAULT_CONFIG` + user YAML |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` — reads user YAML raw |

If a new key is visible in the CLI but not the gateway (or vice versa), you're on the
wrong loader.

### Working directory:
- **CLI** — the process's current directory (`os.getcwd()`).
- **Messaging** — `terminal.cwd` from `config.yaml`; the gateway bridges this to the
  `TERMINAL_CWD` env var for child tools.

## Skin/Theme System

`son_of_anton_cli/skin_engine.py` — data-driven CLI theming; skins are **pure data**.
Built-ins: `default` (classic gold), `ares` (crimson/bronze), `mono` (grayscale),
`slate` (cool blue), `daylight` (light bg), `warm-lightmode` (light bg, warm brown/gold),
`poseidon` (deep blue/seafoam), `sisyphus` (austere grayscale), `charizard`
(volcanic orange/ember). User skins drop into `~/.son-of-anton/skins/<name>.yaml` and
inherit missing values from `default`. Activate with `/skin <name>` or `display.skin` in
config.yaml. See the file for the full key list (colors, spinner faces/verbs/wings, tool
prefix, branding).

## Plugins

Two plugin surfaces; both live under `plugins/` so repo-shipped plugins are discovered
alongside user-installed ones in `~/.son-of-anton/plugins/` and pip entry points.

### General plugins (`son_of_anton_cli/plugins.py` + `plugins/<name>/`)

`PluginManager` discovers plugins from `~/.son-of-anton/plugins/`, `./.son-of-anton/plugins/`,
and pip entry points. Each plugin exposes a `register(ctx)` that can register lifecycle
hooks (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`,
`on_session_end`), tools (`ctx.register_tool(...)`), and CLI subcommands
(`ctx.register_cli_command(...)`).

**Discovery timing pitfall:** `discover_plugins()` only runs as a side effect of importing
`model_tools.py`. Code paths that read plugin state without importing `model_tools.py`
first must call `discover_plugins()` explicitly (idempotent).

**Rule:** plugins MUST NOT modify core files (`run_agent.py`, `cli.py`, `gateway/run.py`,
`son_of_anton_cli/main.py`, etc.). If a plugin needs a capability the framework doesn't
expose, widen the generic plugin surface (new hook, new ctx method) — never hardcode
plugin-specific logic into core.

**No new third-party-product plugins in-tree.** Plugins that integrate someone else's
product (observability backends, vendor SaaS, analytics) ship as standalone plugin repos
users install into `~/.son-of-anton/plugins/` (or via pip entry points). This is a
coupling-and-maintenance decision, not a quality bar. (Directories already in the tree are
existing precedent, not an invitation.)

### Memory-provider plugins (`plugins/memory/<name>/`)

Pluggable memory backends, activated by name via `memory.provider` in config.yaml.
Built-in providers: **honcho, mem0, supermemory, byterover, hindsight, holographic,
openviking, retaindb**. Each implements the `MemoryProvider` ABC (see
`agent/memory_provider.py`), orchestrated by `agent/memory_manager.py`.

**The set of built-in memory providers is closed (policy, May 2026).** New memory backends
ship as standalone plugin repos implementing the same ABC; bug fixes to existing in-tree
providers are welcome.

### Model-provider plugins (`plugins/model-providers/<name>/`)

Inference backend profiles, each calling `providers.register_provider(ProviderProfile(...))`
at module load. The fork ships **custom**; user plugins of the same name
override bundled ones (last-writer-wins). Local endpoints (llama-swap, ollama, vllm) are
configured as `custom_providers` in config.yaml — not as registry entries.

## Skills

Two parallel surfaces:

- **`skills/`** — bundled skills, loadable by default (18 across 4 categories, laid out
  `skills/<category>/<skill>/SKILL.md`).
- **`optional-skills/`** — no longer shipped in the tree. Niche/official-but-inactive
  skills come from the Skills Hub's `OptionalSkillSource` (fetched from the
  `ewtodd/son-of-anton` repo) and are installed explicitly via
  `son-of-anton skills install official/<category>/<skill>`; the adapter lives in
  `tools/skills_hub.py`.

SKILL.md frontmatter: `name`, `description`, `version`, `author`, `license`, `platforms`,
and `metadata.son-of-anton.*` (tags, category, related_skills, config).

Skill authoring standards:
1. `description` ≤ 60 characters, one sentence, ends with a period. No marketing words.
2. Tools referenced in prose must be native Son of Anton tools or MCP servers the skill
   explicitly expects, named in backticks. Don't name shell utilities the agent already
   has wrapped (`grep` → `search_files`, `cat`/`head`/`tail` → `read_file`, `sed`/`awk` →
   `patch`, `find`/`ls` → `search_files target='files'`).
3. `platforms:` gating audited against actual script imports; default to cross-platform.
4. `author` credits the human contributor first.
5. Modern section order: `# <Skill> Skill` title, intro, `## When to Use`, `## Prerequisites`,
   `## How to Run`, `## Quick Reference`, `## Procedure`, `## Pitfalls`, `## Verification`.
6. Scripts in `scripts/`, references in `references/`, templates in `templates/`.
7. Tests for a bundled skill go under `tests/` (e.g. `tests/test_<skill>_skill.py`) —
   stdlib + pytest + `unittest.mock` only.
8. `.env.example` additions isolated to a clearly delimited block.

## Toolsets

All toolsets are defined in `toolsets.py` as a single `TOOLSETS` dict. Each platform's
adapter picks a base toolset; `_SON_OF_ANTON_CORE_TOOLS` is the default bundle most
platforms inherit from: web_search/web_extract, terminal/process, file ops (read_file,
write_file, patch, search_files), vision_analyze, skills (list/view/manage), todo, memory,
session_search, clarify, execute_code, delegate_task, cronjob.

Enable/disable per platform via `son-of-anton tools` (the curses UI) or the
`tools.<platform>.enabled` / `tools.<platform>.disabled` lists in `config.yaml`.

## Delegation (`delegate_task`)

`tools/delegate_tool.py` spawns a subagent with an isolated context + terminal session.
Single (`goal`) or batch (`tasks: [...]`, concurrency capped by
`delegation.max_concurrent_children`). Roles: `leaf` (focused worker, cannot delegate) and
`orchestrator` (can spawn workers; gated by `delegation.orchestrator_enabled`, bounded by
`delegation.max_spawn_depth`).

Durability rule: background `delegate_task` is detached from the current turn but still
process-local. For work that must survive process restart, use `cronjob` or
`terminal(background=True, notify_on_complete=True)` instead.

## Curator (skill lifecycle)

Background skill-maintenance that tracks usage on agent-created skills and auto-archives
stale ones. Archives go to `~/.son-of-anton/skills/.archive/` and are restorable — never
deletes. Only touches skills with `created_by: "agent"` provenance; pinned skills are
exempt from every auto-transition. Config section `curator:`; CLI `son-of-anton curator
<verb>`.

## Cron (scheduled jobs)

`cron/jobs.py` (job store) + `cron/scheduler.py` (tick loop). Agents schedule via the
`cronjob` tool; users via `son-of-anton cron <verb>` or `/cron`. Schedule formats:
duration (`"30m"`), every-phrase (`"every 2h"`), 5-field cron, ISO timestamp (one-shot).

Hardening invariants: 3-minute hard interrupt on cron sessions; catchup window half the
period clamped to 120s–2h; 120s grace for one-shot jobs; file lock at
`~/.son-of-anton/cron/.tick.lock`; cron sessions pass `skip_memory=True`.

## Important Policies

### Prompt Caching Must Not Break

**Do NOT implement changes that would** alter past context mid-conversation, change
toolsets mid-conversation, or reload memories/rebuild system prompts mid-conversation.
The ONLY time we alter context is during context compression.

Slash commands that mutate system-prompt state (skills, tools, memory, etc.) must be
**cache-aware**: default to deferred invalidation (change takes effect next session),
with an opt-in `--now` flag for immediate invalidation.

### Background Process Notifications (Gateway)

`terminal(background=true, notify_on_complete=true)` triggers a watcher that detects
process completion and triggers a new agent turn. Verbosity via
`display.background_process_notifications`: `concise` (default), `all`, `result`, `error`, `off`.

## Home Scoping (`SON_OF_ANTON_HOME`)

The multi-instance **profile** system was deleted (commit `2067d1be`, "delete the profiles
system"): there is no `/profile` command, no `_apply_profile_override()`, no
`_get_profiles_root()`, and `son_of_anton_cli/profiles.py` (plus the profile gateway
modules) are gone. `tests/test_no_profiles.py` guards that none of those modules become
importable again.

The only home-scoping mechanisms that remain are:

1. **`SON_OF_ANTON_HOME` env var** — the scope the process was launched under.
   `get_son_of_anton_home()` resolves it (falling back to `~/.son-of-anton`).
2. **In-process override** — `set_son_of_anton_home_override()` /
   `get_son_of_anton_home_override()` in `son_of_anton_constants.py` for context-local
   scoping; it deliberately does not mutate `os.environ`.

Rules that still hold for home-safe code:
1. **`get_son_of_anton_home()` for all SON_OF_ANTON_HOME paths** — never hardcode
   `~/.son-of-anton` or `Path.home() / ".son-of-anton"`.
2. **`display_son_of_anton_home()` for user-facing messages.**
3. Module-level constants are fine — they cache the home at import time, which is after
   the env var is available.

## Known Pitfalls

### DO NOT hardcode `~/.son-of-anton` paths
Use `get_son_of_anton_home()` for code paths, `display_son_of_anton_home()` for
user-facing output. Hardcoding ignores a configured `SON_OF_ANTON_HOME`.

### All CLI menu-pickers MUST use curses.
Interactive menus must use `son_of_anton_cli/curses_ui.py`. See
`son_of_anton_cli/tools_config.py` for an example.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores it around subagent
execution. Code reading this global may see stale values during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name — those tools
may be unavailable. If a cross-reference is needed, add it dynamically in
`get_tool_definitions()` in `model_tools.py`.

### The gateway has TWO message guards — both must bypass approval/control commands
See "Gateway" above.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an unused module
into a live code path, E2E test the real resolution chain with actual imports against a
temp `SON_OF_ANTON_HOME`.

### Tests must not write to `~/.son-of-anton/`
The `_isolate_son_of_anton_home` autouse fixture in `tests/conftest.py` redirects
`SON_OF_ANTON_HOME` to a temp dir. Never hardcode `~/.son-of-anton/` paths in tests.

## Testing

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/agent/                     # one directory
scripts/run_tests.sh tests/agent/test_foo.py -k test_x  # one test
```

The suite is deliberately small — it covers the fork's load-bearing contracts (router
classification, provider-catalog invariants, physics-mode imports/endpoint resolution/
formal evaluation, config merge, profile isolation, slash-command registry invariants,
core-tool registration, skin fallback). Tests for removed features were pruned with the
features. Add tests when fixing a bug or adding a feature, not as snapshots of current data.

### Pre-commit hooks

`.pre-commit-config.yaml` wires the local gates (no CI by design — they are fast):

```bash
nix develop -c pre-commit install   # one time
```

- `ruff check` — the repo's single enabled rule (PLW1514, explicit `encoding=`)
- `scripts/run_tests.sh` — the full contract suite (runs in seconds)

### Don't write change-detector tests

A test is a **change-detector** if it fails whenever data that is **expected to change**
gets updated — model catalogs, config version literals, enumeration counts. Assert
invariants ("every model in the catalog has a context length") instead of snapshots
("catalog has exactly N entries").

### Never read source code in tests

A test that reads a source file's text is testing the shape of the source, not its
behavior. Extract the logic into a small pure/DI-testable function and call it for real.

### Don't fake the host OS

Use `@pytest.mark.linux_only` / `macos_only` / `windows_only` markers, never a bare
`skipif` — the CI classifier greps for the marker *name*. Pure functions that take a
platform as data can stay unmarked.

## Operational Notes

- **Verification:** `nix flake check` (package + modules + venv import sweep). The venv
  import sweep imports the full surface (98 modules incl. both physics modes) — it catches
  import breakage `compileall` can't.
- **Upstream refs:** hermes-agent v0.20.5 (`fcbd1076a9`), physics-intern (`5553bb6`).
  The fork's history is a rewrite; when hunting upstream behavior, reference the upstream
  repos directly.
- **The agent name:** the GitHub account is `son-of-anton-bot`, and it is the only
  author on a commit — see "Commit convention" above.
- **Known loose ends** (tracked in `STATUS.md`): the credential-gated deep-Nous tail
  has been excised — `son_of_anton_cli/auth.py`'s Nous OAuth/portal flow and the
  `agent/auxiliary_client.py` Nous branch are gone (`nous_account.py`,
  `agent/portal_tags.py`, root `models.py`, and the proxy's `nous_portal.py` adapter
  were already gone). Only stale prose remains: the gateway relay's enroll docstring
  still names `resolve_nous_access_token()` (never called in code), and `openrouter`
  stays a live provider (used by `tools/openrouter_client.py`). Physics runs are
  synchronous turns.
