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

An always-on agent you talk to from a terminal or from Signal.

One daemon per account, `soa-d`, written in Go, holds the sessions, runs the
agent loop and its tools, serves Signal and runs scheduled jobs. The terminal
UI and the `son-of-anton` command are its clients. It speaks to one
OpenAI-compatible endpoint (here, [maitred](https://github.com/ewtodd/maitred)
in front of local models). Linux and Nix only.

## Status

Everything runs on soa-d. There is no Python left; the physics mode is being rebuilt in Go.
Versions are dates: `son-of-anton --version` prints the day the build's
commit was made, as `YY.MM.DD`.

## The command

```
son-of-anton                    the terminal UI (-c continues here, -r ID|latest resumes)
son-of-anton "a question"       one answer, printed (also -q, --query-file FILE|-)
son-of-anton -z "a task"        a delegated one-shot: lean prompt, no memory
son-of-anton status             which daemon serves this home, Signal, cron, models
son-of-anton sessions ...       list, rename, delete, import (Claude Code, Codex, opencode)
```

`-m MODEL`, `--reasoning LEVEL` and `--yolo` apply to all three ways in. A
one-shot denies dangerous commands unless `--yolo`, since nobody is there to
approve them.

## The terminal UI

A port of [opencode](https://github.com/sst/opencode)'s session layout: the
transcript, a sidebar on wide screens (`ctrl+b`), the prompt below.

- `ctrl+p` opens the command palette, `ctrl+g` your editor, `shift+tab`
  cycles the permission mode (default, ask, lockdown, yolo), `ctrl+x ↓` opens
  the newest subagent (`←`/`→` between them, `↑` back), `ctrl+t` expands or
  folds the newest tool row.
- `/model`, `/reasoning`, `/resume` and `/cancel` open pickers; `/new`,
  `/compact`, `/commit`, `/stop`, `/sendnow`, `/sendall`, `/status`, `/skin`.
  `!cmd` runs a shell command without a model turn. The `/model` picker lists
  only the models the gateway is serving, and picking one opens its reasoning
  picker right after (`/reasoning` still works on its own; a session whose
  model is down is told `<model> is not being served; pick another model`,
  in a chat as a message, once per outage). Naming an unloaded model with
  `/model <name>` still works. A tool row folds to one line; click it (or
  `ctrl+t`) to see its output. The sidebar keeps the session's todo checklist.
- A message typed during a turn queues in the daemon, survives a restart and
  goes in as soon as the running tools end, or when the model answers with no
  more tool calls. `/sendnow` sends it sooner, cutting off a reply in progress
  but still letting a running tool finish; so does `ctrl+enter` on a terminal
  with the [kitty keyboard protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/).
- Under a routed model (maitred's `auto`), the meta row and the context bar
  show the model that answered and its window.

## Signal

Each instance answers its own Signal groups (all can share one signal-cli
number). The chat's agent is **fiona**, the front desk: she answers, looks
things up, reads images and keeps her own notes and memory, and hands real
work (code, files, commands) to a **worker**, a terminal session in the
directory you name, through her `relay` tool. Workers keep
`[relay] worker_hours`; their approvals and questions come to the chat as
`/approve <id>` and `/deny <id>`. `/pickup` in the terminal UI takes a worker
over.

fiona runs on `gateway.model` (here maitred's `fiona` route: whatever eklow
serves) and falls back to `model.default` when it is down. Each message tells
her which model answered the last one. In a chat: `/new`, `/stop`, `/status`,
`/model [name|default]`, `/sendnow`, `/sendall`, `/approve`, `/deny`. When her
model is not being served, the chat is told once per outage
(`<model> is not being served; pick another model`). Subagents a session
delegates work to run on the parent's model, or `[delegation] model` when you
set one; a `task` call may pin `model` for that call.

## Memory

TOML under `~/.son-of-anton/memories/`, addressed by id:

- **Rules** (`rules.toml`): standing instructions in the system prompt,
  frozen for a session. Every change needs your approval; a full store
  refuses rather than evicts.
- **Facts**: knowledge recalled when its topic comes up, never in the
  system prompt.

The terminal never sees the chat's memory (`scope = "gateway"`), and
`session_search` never finds a chat from the terminal. With `memory.rag`,
recall and `session_search` also match by meaning.

## Scheduled jobs

The agent schedules with its `cronjob` tool; with `[daemon] cron = true` the
daemon runs the jobs and delivers to the chat that asked.

## Physics

The physics mode is being rebuilt in Go (daemon/phases/11b); `son-of-anton
problem` says so and exits 2. The interpreter it will run model-written code
under is kept: `nix build .#physics-runtime`.

## NixOS

```nix
services.son-of-anton.instances.work = {
  enable = true;
  user = "e-work";
  son-of-antonHome = "/home/e-work/.son-of-anton";
  environmentFiles = [ config.age.secrets.son-of-anton-work-env.path ];
};
```

Each instance is a `soa-d serve` unit. `settings` is merged into `config.toml`
three ways (dropping a key from Nix removes it, runtime writes survive), and a
rebuild that changes the settings restarts the unit. `memoryRules` or
`memoryRulesFile` seed `rules.toml` the same way. An instance for a group with
other people gets its own service account (`createUser = true`); on a
person's own account (`managedAccount`) the unit drops its filesystem sandbox,
since it is that person's terminal agent too. Nothing soa-d runs can gain
privileges: no sudo, on any instance.

## Development

```bash
cd daemon && go test ./...    # the gates are in daemon/AGENTS.md
nix flake check               # packages and modules
```

## Provenance

Son of Anton began as a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5, and its physics mode began as a port of
[huggingface/physics-intern](https://github.com/huggingface/physics-intern).
The Python is gone, the physics mode included: soa-d and its terminal UI are a
new implementation in Go, written against a spec with the Python as the
behavioural reference, not a line-by-line port. Since November 2026 none of
hermes-agent's code or text remains: the prompts, tool descriptions,
messages, command-safety rules, skins and Nix packaging were rewritten from
what they have to do. Only names were kept (options, config keys,
environment variables), so existing setups keep working.

Two things are still borrowed. The terminal UI's layout is a port of
[opencode](https://github.com/sst/opencode)'s session view, and some of its
code follows opencode's closely (the palette, the subagent footer, small
formatters); that stays. No code of physics-intern remains. The daemon design and
permission modes come from the archived
[temple](https://github.com/ewtodd/temple) harness.

## AI Full Disclosure

This software is developed with strong assistance from AI, with humans leading
the ideas, testing, and debugging. This project is not my day job, so it is
developed with as little actual coding from me as possible. If you are not
happy with AI-developed code, this software is not for you. (This statement
inspired by that of [antirez/ds4](https://github.com/antirez/ds4).)

## License

[0BSD](LICENSE), © 2026 Ethan Todd. The parts of the terminal UI that follow
opencode stay under opencode's MIT license; LICENSE has its notice.
hermes-agent is the work of Nous Research, physics-intern of HuggingFace and
opencode of SST.
