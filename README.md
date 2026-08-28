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

An always-on agent you can talk to from a terminal or from Signal. It has three
modes: the normal agent loop, a physics research loop, and a multi-agent
research pipeline.

## Provenance

Son of Anton is a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (2026.8.19, upstream commit `fcbd1076a9`), stripped down to a smaller
daemon, and extended with the physics modes ported from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`). Both projects are MIT licensed and so is this fork.
Hermes's learning loop (skills, memory, session search, cron) is kept.

It replaces the archived [temple](https://github.com/ewtodd/temple) harness.
The daemon design, permission modes, and request router come from there.

## What it does

Three agent modes. A router picks one per request, and `/mode` overrides it:

- `standard`: the hermes loop, terminal, files, web, skills, memory,
  delegation, cron.
- `physics`: one research manager with append-only memory, a windowed
  scratchpad, a token budget, and a `submit_final_answer` tool, working in a
  git-versioned scratch directory.
- `research`: a nine-agent pipeline (surveyor, planner, orchestrator,
  researcher, computer, reviewer, critic, adjudicator, formatter) over a
  shared `ResearchState`, also in a git-versioned directory.

The router only classifies the **first** message of a session. Everything after
that stays in the standard loop, which is the one that keeps history. Physics
and research are one-shot: they only see the message that started them, so
letting a follow-up drop into them looked exactly like the agent forgetting the
conversation.

Physics and research runs get scored by numeric checks against the problem
spec. The model writes real analysis code (ROOT or Python), runs it, and writes
results to `RESULTS.txt`. There are self-contained toy problems in `problems/`.

You can switch modes off per deployment with `router.modes`. A gateway for
household chores has no use for the physics loop, and leaving it on is worse
than clutter, because a stray "how long does that take to decay" can drop
someone into a one-shot research run:

```yaml
router:
  modes: [standard]                          # household: nothing else offered
  # modes: [standard, physics, research]     # the default, everything on
```

With a mode off, its keywords stop routing, `/mode` will not accept it and does
not list it, and a session that was pinned to it before the change falls back
to standard. Leave the key out entirely and you get all three, so existing
configs are unaffected.

Other things worth knowing:

- Talks over Discord, Slack, and Signal. One gateway process also runs cron.
- Same agent core in the CLI and the TUI.
- `/perm default|ask|lockdown|yolo` sets how much it asks before running
  commands. Some things stay blocked even under yolo.
- `/model auto` routes each request to a model slot; `/model NAME` pins one for
  the session.

## Running it

```bash
nix build      # sealed uv2nix venv, wrapper in result/bin/
nix run .# --  # start the CLI
```

It reads `~/.son-of-anton/config.yaml` for settings and `~/.son-of-anton/.env`
for secrets. A local model endpoint is configured like any other
OpenAI-compatible provider:

```yaml
model:
  default: qwen3.8-27b-coding   # what the CLI opens with
  provider: custom
gateway:
  model: gemma-4-26b            # what the gateway answers with, if different
custom_providers:
  custom:
    base_url: http://127.0.0.1:8080/v1
physics:
  model: qwen3.8-27b-coding
  base_url: http://127.0.0.1:8080/v1
```

## One service per account

The NixOS module runs a separate gateway for each account, as a system service,
so they start at boot without anyone logging in. Each one runs *as* that
account with `SON_OF_ANTON_HOME=~/.son-of-anton`.

Sharing the home is what makes this useful. The service and your own
`son-of-anton` in a terminal end up on one `state.db`. A conversation you start
on Signal is sitting there in `/sessions` when you open the CLI, and you can
pick it up. There is nothing to sync because there is only one copy.

```nix
services.son-of-anton.instances = {
  work = {
    enable = true;
    user = "e-work";
    managedAccount = true;                 # it's a real login account
    son-of-antonHome = "/home/e-work/.son-of-anton";
    workingDirectory = "/home/e-work";
    environmentFiles = [ config.age.secrets.son-of-anton-work-env.path ];
  };
  play = {
    enable = true;
    user = "e-play";
    managedAccount = true;
    son-of-antonHome = "/home/e-play/.son-of-anton";
    workingDirectory = "/home/e-play";
    environmentFiles = [ config.age.secrets.son-of-anton-play-env.path ];
  };
};
```

`managedAccount = true` matters. Without it the module creates and chmods the
directories it manages, which against a real home means `2770` on
`/home/e-work`. That makes sshd's `StrictModes` stop accepting your
`authorized_keys`, and it hands everyone else in your primary group write
access to your whole home. With it, the module only touches `~/.son-of-anton`.

### Everyone shares one Signal number

All the services talk to one signal-cli daemon and all of them see every
message, because signal-cli broadcasts events over SSE. What separates them is
the group: each service answers exactly one Signal group and drops everything
else before it does any work.

So set up one group per service, put yourself and the bot in it, and give each
service its group id:

```bash
curl -s -X POST http://YOUR-SIGNAL-HOST:7583/api/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"listGroups","params":{},"id":"1"}' \
  | jq -r '.result[] | "\(.name)\t\(.id)"'
```

Put the id in that instance's env file as `SIGNAL_GROUP_ALLOWED_USERS`, along
with `SIGNAL_ALLOWED_USERS`, the list of people allowed to talk to it. That
list is checked on group messages too, so it has to include you.

Direct messages are dropped by default (`SIGNAL_DM_MODE=ignore`). A DM has no
group id, so there is no way to tell which service should answer it. Leave it
on and one DM gets you one reply from every running service.

If your signal-cli runs on a different machine, note that it opens attachment
paths on **its own** filesystem, so a file the gateway just wrote is not there.
The gateway retries those sends with the file inlined as a data URI, which is
capped at 16 MB.

### A group with other people in it

You can run an instance for a shared group, like household planning with a
partner. Give it its own service account rather than a personal one:

```nix
services.son-of-anton.instances.house = {
  enable = true;
  user = "soa-house";
  createUser = true;
  stateDir = "/var/lib/soa-house";
  son-of-antonHome = "/var/lib/soa-house/.son-of-anton";
  workingDirectory = "/srv/household";
  environmentFiles = [ config.age.secrets.son-of-anton-house-env.path ];
  extraPackages = [ pkgs.pandoc pkgs.typst ];
  settings.router.modes = [ "standard" ];   # no physics/research here
};
```

Its env file lists everyone in the group under `SIGNAL_ALLOWED_USERS`. Anyone
not listed gets dropped even if they are in the group.

Use a service account here, not your own. The agent acts for whoever is
talking, so running it as `e-play` would hand the other people in that group
`e-play`'s home directory, ssh keys, and git identity. Its working directory is
the only thing it can reach.

`extraPackages` is per-instance and lands on that service's `PATH`, which is
how you give one instance tools the others don't have.

A note on pandoc and PDFs: a plain `pandoc in.md -o out.pdf` fails under a
systemd service. pandoc's typst template writes `font: <mainfont>` and typst
rejects an empty font list, and the service has no fontconfig so typst finds no
fonts at all. Wrap it:

```nix
md2pdf = pkgs.writeShellApplication {
  name = "md2pdf";
  runtimeInputs = [ pkgs.pandoc pkgs.typst ];
  text = ''
    out="''${2:-''${1%.*}.pdf}"
    TYPST_FONT_PATHS=${pkgs.dejavu_fonts}/share/fonts \
      pandoc "$1" -o "$out" --pdf-engine=typst -V mainfont="DejaVu Sans"
    echo "$out"
  '';
};
```

### Home Manager

There is also a Home Manager module, if you would rather run the gateway as a
user service. Enable linger or it stops when you log out.

```nix
imports = [ son-of-anton.homeManagerModules.default ];
services.son-of-anton = {
  enable = true;
  gateway.enable = true;
  environmentFiles = [ config.age.secrets."son-of-anton-env".path ];
};
users.users.YOUR-ACCOUNT.linger = true;
```

## Commands

| Command | What it does |
|---|---|
| `/mode auto\|standard\|physics\|research` | pin the session's agent mode |
| `/model auto\|NAME` | turn routing back on, or pin a model |
| `/perm default\|ask\|lockdown\|yolo` | set the permission mode |
| `/sessions` | list sessions, including ones started on Signal |
| `/q`, `:q`, `/exit` | quit the CLI |

`/help` lists the rest.

## Development

```bash
nix flake check   # package, both modules, venv import smoke
nix develop       # python dev shell with the editable venv
```

Run the test suite through the dev shell (`nix develop -c scripts/run_tests.sh`);
it needs `SON_OF_ANTON_PYTHON` from the shell hook.

One trap worth knowing if you edit and re-run in a loop: an edit that keeps a
file the same size, made within the same second as the last one, can leave
CPython using the old `.pyc`, because invalidation is on mtime plus size. Clear
`__pycache__` between runs.

## License

MIT. Hermes Agent and PhysicsIntern are the work of Nous Research and
HuggingFace; see the upstream repositories for their contributor lists.
