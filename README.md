# SON OF ANTON

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

An always-on agent harness with three modes: the standard agent loop, a
single-agent physics loop, and a critical self-research pipeline.

## Provenance

Son of Anton is a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (2026.8.19, upstream commit `fcbd1076a9`), stripped to a lean daemon
surface, and extended with the physics modes ported from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`). Both projects are MIT licensed; this fork remains MIT.
Hermes's learning loop — skills, memory, session search, cron — is retained.

It is the second iteration of the archived
[temple](https://github.com/ewtodd/temple) harness; the daemon design,
permission modes, and request router carry over from it.

## What it is

- **Three agent modes**, selected per request by a heuristic router with a
  `/mode` override:
  - `standard` — the hermes agent loop: terminal, files, web, skills, memory,
    delegation, cron.
  - `physics` — the Autophysicist loop: a single research manager with
    append-only permanent memory, a windowed scratchpad, a token budget, and
    a `submit_final_answer` tool, iterating in a git-versioned workspace.
  - `research` — the nine-agent pipeline: surveyor, planner, orchestrator,
    researcher, computer, reviewer, critic, adjudicator, formatter, over a
    structured `ResearchState` in a git-versioned workspace.
- **Experimental verification**: physics and research runs are scored by
  numeric checks against the problem spec — the model writes real analysis
  code (ROOT or Python), runs it, and reports results in `RESULTS.txt`.
  See `problems/` for self-contained toy problems.
- **Three platforms**: Discord, Slack, and Signal, from one gateway process
  that also runs the cron scheduler.
- **CLI and TUI** with the same agent core.
- **Per-user daemons**: the Home Manager module runs one gateway per account
  (work/play) under its own `systemd` user service with its own state.
- **Permission modes**: `/perm default|ask|lockdown|yolo` — default (smart
  approvals), ask (manual approval), lockdown (every command needs a human),
  yolo (skip approvals). Hardline blocks still apply under yolo.
- **Model routing**: `/model auto` classifies each request to a model slot;
  `/model NAME` pins a session.

## Quick start (Nix)

```bash
nix build                    # sealed uv2nix venv + wrapper in result/bin/
nix run .# --                # start the CLI

# Per-account daemon via the Home Manager module:
#   imports = [ son-of-anton.homeManagerModules.default ];
#   services.son-of-anton = {
#     enable = true;
#     gateway.enable = true;
#     settings.terminal.cwd = "/home/e-play/work";
#     environmentFiles = [ config.age.secrets."son-of-anton-env".path ];
#   };
#   users.users.<account>.linger = true;   # keep the user service alive
```

The `son-of-anton` binary reads `~/.son-of-anton/config.yaml` (settings) and
`~/.son-of-anton/.env` (secrets only). Local model endpoints are configured
like any OpenAI-compatible provider:

```yaml
model:
  default: deepseek-v4
  provider: custom
custom_providers:
  custom:
    base_url: http://127.0.0.1:8080/v1
physics:
  model: deepseek-v4
  base_url: http://127.0.0.1:8080/v1
```

## Modes and commands

| Command | Effect |
|---|---|
| `/mode auto\|standard\|physics\|research` | pin the session's agent mode |
| `/model auto\|NAME` | re-enable routing or pin a model |
| `/perm default\|ask\|lockdown\|yolo` | set the permission mode |
| `/q`, `:q`, `/exit` | quit the CLI |

Physics keywords ("fit the histogram", "half-life", "cross-section", ...)
route to `physics`; research keywords ("derive the", "literature review",
...) route to `research`; everything else uses the standard loop.

## Development

```bash
nix flake check   # package + modules + venv import smoke
nix develop       # python dev shell with the editable venv
```

## License

MIT. Hermes Agent and PhysicsIntern are the work of Nous Research and
HuggingFace respectively; see the upstream repositories for their
contributor lists.
