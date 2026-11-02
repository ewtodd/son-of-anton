```
███████╗  ██████╗  ███╗   ██╗      ██████╗  ███████╗
██╔════╝ ██╔═══██╗ ████╗  ██║     ██╔═══██╗ ██╔════╝
███████╗ ██║   ██║ ██╔██╗ ██║     ██║   ██║ █████╗
╚════██║ ██║   ██║ ██║╚██╗██║     ██║   ██║ ██╔══╝
███████║ ╚██████╔╝ ██║ ╚████║     ╚██████╔╝ ██║
╚══════╝  ╚═════╝  ╚═╝  ╚═══╝      ╚═════╝  ╚═╝
 █████╗   ███╗   ██╗ ████████╗  ██████╗  ███╗   ██╗
██╔══██╗  ████╗  ██║ ╚══██╔══╝ ██╔═══██╗ ████╗  ██║
███████║  ██╔██╗ ██║    ██║    ██║   ██║ ██╔██╗ ██║
██╔══██║  ██║╚██╗██║    ██║    ██║   ██║ ██║╚██╗██║
██║  ██║  ██║ ╚████║    ██║    ╚██████╔╝ ██║ ╚████║
╚═╝  ╚═╝  ╚═╝  ╚═══╝    ╚═╝     ╚═════╝  ╚═╝  ╚═══╝
```

An always-on agent you talk to from a terminal or from Signal, with a
separate physics research mode that runs only when you ask for it.

## Status: the Go rewrite

The Python backend is being replaced, one phase at a time, by `soa-d`, a Go
daemon per instance (`daemon/`; the plan is `daemon/SPEC.md`).

- **On soa-d now:** the terminal TUI (`son-of-anton`), `-q` and `-z`.
- **Still Python:** the Signal gateway and its relay workers, cron, physics
  runs.
- **Not in the daemon yet:** compaction, titles, the memory tool, recall and
  `session_search` (phase 3); subagents (4); `/handoff` and `/pickup` (5); MCP
  servers and web tools (7); the Python sessions in `state.db` (imported in 8).

## Running it

```bash
nix build            # result/bin/son-of-anton (soa-d is on its PATH)
nix run .# --        # the TUI, on a new session in this directory
nix run .# -- -c     # continue this workspace's latest session
```

Settings live in `~/.son-of-anton/config.toml`, secrets in `.env` next to it.
One OpenAI-compatible provider serves everything:

```toml
[model]
default = "qwen3.8-27b"
provider = "custom"

[custom_providers.custom]
base_url = "http://127.0.0.1:8080/v1"
key_env = "MY_ENDPOINT_KEY"     # read from the environment, then .env
```

The TUI is a client of the daemon. `son-of-anton` starts `soa-d serve` when
none is running (log: `~/.son-of-anton/logs/soa-d.log`); sessions, queued
messages and pending approvals live in the daemon, so quitting the TUI leaves a
running turn going. After an update, `pkill -f 'soa-d serve'` and the next
launch starts the new daemon.

## Sessions and the command line

- `-c` continues the latest session of this workspace (the git root, or the
  directory itself) and never another directory's; with none it starts a new
  one. `--resume ID` opens a session, `--resume latest` the newest anywhere.
- `-q PROMPT` answers one query. `-z PROMPT` is a one-shot for scripts: stdout
  is only the final answer, `--usage-file F` writes token use, and the prompt
  is lean (no identity or memory, only rules marked `delegated`). Both take
  `-m MODEL`, `--reasoning LEVEL` and `--yolo`; without `--yolo` a dangerous
  command is denied, since nobody is there to approve it.
- A message typed while a turn runs is queued in the daemon and survives a
  restart.
- The TUI's sessions are in `soa-d.db`; the gateway's and cron's are still in
  `state.db`.

## Commands

| Command | What it does |
|---|---|
| `/model [NAME]` | switch the session's model (bare: a picker) |
| `/reasoning [LEVEL]` | set the reasoning effort (bare: a picker) |
| `/perm default\|ask\|lockdown\|yolo` | how much it asks before running commands; shift+tab cycles |
| `/new`, `/clear` | start a new session here |
| `/resume [ID]`, `/sessions` | open a session (bare: a picker) |
| `/commit [notes]` | review the uncommitted diff, write a message in the repo's style, commit |
| `/stop` | kill the session's background processes |
| `/sendnow [text]`, `/sendall` | steer the running turn onto this text or the oldest queued message; `/sendall` onto all of them |
| `/cancel` | drop queued messages |
| `/status`, `/help`, `/skin`, `/q` | |

`!cmd` runs a shell command in the session's directory. `/commit` authors as
`[git] author_name` / `author_email` when both are set, and leaves you as the
committer.

## Signal

Each instance's gateway answers one Signal group (all instances can share one
signal-cli number) and also runs cron. On an instance with
`gateway.single_user = true` the chat is a relay: project work goes to a
headless terminal session in the directory you name (`son-of-anton chat -Q`),
the chat reports each result, and a dangerous command asks there as
`/approve <id>` / `/deny <id>`. Sessions started in the TUI cannot be relayed
until phase 5.

## Memory

Two kinds, as TOML under `~/.son-of-anton/memories/`, addressed by id:

- **Rules** (`rules.toml`): standing instructions, in the system prompt and
  frozen for a session. Every change needs your approval, and a full store
  refuses rather than evicts. `scope = "gateway"` keeps a rule on Signal;
  `delegated = true` keeps it even in lean `-z` prompts.
- **Facts** (`facts.toml`, `facts.gateway.toml`): knowledge recalled when its
  topic comes up, never in the system prompt. Recall is on the Python side
  only until phase 3b.

The terminal never sees gateway memory. On NixOS, `memoryRules` seeds
`rules.toml` (a three-way merge by id that never clobbers your edits), and
`memoryRulesFile` takes rules from a runtime path such as an agenix secret.
`memory.rag` adds embedding recall of facts and notes, and lets
`session_search` match by meaning.

## Physics runs

Nothing routes a chat message into physics. `son-of-anton problem create`
writes a spec for a dataset; `problem run` runs it and prints `ANSWER.md` and
`FORMAL_EVAL.md`. A chat agent can drive both through its terminal.

```bash
son-of-anton problem create --data ~/lab-data/run42 \
    --goal "measure the half-life" -o problems/run42/problem.toml
son-of-anton problem run problems/run42/problem.toml --max-iterations 5
```

- One research manager (memory, scratchpad, token budget,
  `submit_final_answer`) dispatches sub-agents that run code, in a
  git-versioned workspace; a critic reviews each iteration
  (`physics.critique_every_n`).
- Model code runs in a separate scientific interpreter (`nix build
  .#physics-runtime`, then `physics.python`) under bubblewrap: no network, no
  home, no secrets, data mounted read-only from `physics.data_dirs`. It fails
  closed without `bwrap`.
- `physics.model` reasons, `physics.coder_model` writes the scripts, and
  `physics.mcp.roles` limits which lookups each role may use.
- Set `--max-iterations` on a first run: there is no other ceiling (default 50).
  Raise `--script-timeout` (default 60s) for large data.

## Cron

`son-of-anton cron <verb>` or `/cron` manages jobs, and the agent schedules
with its `cronjob` tool. A job without a pinned model is skipped, with an
alert, when the global default has changed since it was made, so an unattended
job never drifts onto a paid model. Set `[cron] model_drift_guard = false` if
your models cost nothing.

## Deploying on NixOS

The NixOS module runs one gateway per account as a system service, with
`SON_OF_ANTON_HOME=~/.son-of-anton`, so the service and your terminal share a
home:

```nix
services.son-of-anton.instances.work = {
  enable = true;
  user = "e-work";
  son-of-antonHome = "/home/e-work/.son-of-anton";
  environmentFiles = [ config.age.secrets.son-of-anton-work-env.path ];
};
```

- An instance for a group with other people gets its own service account
  (`createUser = true`); `git.github` gives one a key for GitHub.
- `settings` is merged into `config.toml` three ways, so dropping a key from
  Nix removes it and runtime writes survive; `pruneUnmanagedSettings = true`
  makes the file purely declarative.
- A Home Manager module covers a terminal-only install.
- There are no installation commands (`setup`, `update`, `config set`,
  `gateway install`): the module declares the deployment and `systemctl` runs
  it.

## Development

```bash
nix flake check                      # packages, modules, venv import sweep
nix develop -c scripts/run_tests.sh  # the Python suite (never bare pytest)
```

The daemon's gates are in `daemon/AGENTS.md`, and the repo's rules for agents
and contributors in `AGENTS.md`.

## Provenance

A hard fork of [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (`fcbd1076a9`), stripped to Signal and one OpenAI-compatible provider,
and now being rewritten in Go. The physics mode began as a port of
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(`5553bb6`) and has been rebuilt for experimental data. The daemon design and
permission modes come from the archived [temple](https://github.com/ewtodd/temple)
harness. The TUI is a from-scratch Textual port of
[opencode](https://github.com/sst/opencode)'s session layout
(`TUI_AESTHETICS.md` maps it file by file). The session journal follows
[goluckyryan/letsClaw](https://github.com/goluckyryan/letsClaw).

## AI Full Disclosure

This software is developed with strong assistance from AI, with humans leading
the ideas, testing, and debugging. This project is not my day job, so it is
developed with as little actual coding from me as possible. If you are not
happy with AI-developed code, this software is not for you. (This statement
inspired by that of [antirez/ds4](https://github.com/antirez/ds4).)

## License

MIT. Hermes Agent and physics-intern are the work of Nous Research and
HuggingFace; letsClaw is the work of goluckyryan; opencode is the work of SST.
See the upstream repositories for their contributor lists.
