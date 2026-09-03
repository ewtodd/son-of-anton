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
<!---->
An always-on agent you can talk to from a terminal or from Signal.
It has two
modes: the normal agent loop, and a physics research loop.
<!---->
## Provenance
<!---->
Son of Anton is a hard fork of
[Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (2026.8.19, upstream commit `fcbd1076a9`), stripped down to a smaller
daemon. Hermes's learning loop (skills, memory, session search, cron) is kept.

The Autophysicist mode began as a port of
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`) and has since taken its own path. physics-intern was built
for theoretical physics, where a sub-agent derives a result and there is
nothing to look up; this one is built for experimental data, where the work is
calibrating a detector and training a classifier on recorded waveforms, and the
dominant failure is guessing at a library API. What that changed: computations
run under a separate scientific interpreter inside a bubblewrap sandbox with
the lab's data mounted read-only; the Manager can read its own workspace and
look up literature; sub-agents can pull documentation; the reasoning and coding
roles can run on different models; and each iteration is reviewed from outside
by a critic. physics-intern also shipped a nine-agent research pipeline
(surveyor, planner, orchestrator, researcher, computer, reviewer, critic,
adjudicator, formatter); it was ported and then removed as redundant, once the
Autophysicist carried its own critic, its own sub-agent dispatch, and its own
verdict-free review. The debt is real and gladly acknowledged — the
architecture is no longer theirs. Both upstream projects are MIT licensed and
so is this fork.
<!---->
It replaces the archived [temple](https://github.com/ewtodd/temple) harness.
The daemon design, permission modes, and request router come from there.
<!---->
The terminal interface is modeled on
[opencode](https://github.com/sst/opencode), whose TUI is the nicest-looking
one in this category and, being open source, could be read rather than guessed
at. The layout is a deliberate port of its session route: the transcript column
beside a 42-column sidebar, the left rail on messages and the prompt, the
two-column tool icons, the identity row under the prompt, and the "system"
theme that asks the terminal for its own background and generates its surfaces
from it, so the interface belongs to whatever theme you already run. The code
is reproduced entirely (Python and Textual, against a different agent loop); the design is
theirs. opencode is MIT licensed. See `TUI_AESTHETICS.md` for the mapping,
file by file.
<!---->
<!---->
## AI Full Disclosure
<!---->
This software is developed with strong assistance from AI, with humans leading the ideas, testing, and debugging.
This project is not my day job, so it is developed with as little actual coding from me as possible.
If you are not happy with AI-developed code, this software is not for you.
(This statement inspired by that of [antirez/ds4](https://github.com/antirez/ds4).)
<!---->
## What it does
<!---->
Two agent modes.
A router picks one from the first message of a session, and `/mode` overrides
it on any turn:
<!---->
- `standard`: the hermes loop, terminal, files, web, skills, memory,
  delegation, cron.
- `physics`: the Autophysicist — one research manager with append-only memory,
  a windowed scratchpad, a token budget and a `submit_final_answer` tool,
  dispatching ephemeral sub-agents that run code in a sandbox, with each
  iteration reviewed by a critic. Works in a git-versioned scratch directory.
<!---->
The router only classifies the **first** message of a session.
Everything after
that stays in the standard loop, which is the one that keeps history.
Physics
is one-shot: it only sees the message that started it, so
letting a follow-up drop into it looked exactly like the agent forgetting the
conversation.
<!---->
Physics runs get scored by numeric checks against the problem
spec.
The model writes real analysis code, runs it in a sandbox, and writes
results to `RESULTS.txt`.
There are self-contained toy problems in `problems/`,
`son-of-anton problem create` writes a spec for a real dataset from a data
directory and a one-line goal, and `son-of-anton problem run` runs one (see
[Physics runs](#physics-runs)).
<!---->
You can switch the mode off per deployment with `router.modes`.
A gateway for
household chores has no use for the physics loop, and leaving it on is worse
than clutter, because a stray "how long does that take to decay" can drop
someone into a one-shot physics run:
<!---->
```yaml
router:
  modes: [standard]                          # household: nothing else offered
  # modes: [standard, physics]               # the default, everything on
```
<!---->
With the mode off, its keywords stop routing, `/mode` will not accept it and does
not list it, and a session that was pinned to it before the change falls back
to standard.
Leave the key out entirely and you get both, so existing
configs are unaffected.
<!---->
Other things worth knowing:
<!---->
- Talks over Discord, Slack, and Signal.
One gateway process also runs cron.
- The Textual TUI is the only interface. There is no `--tui` flag and no
  fallback REPL; `son-of-anton "..."` still answers a single query without it.
- `/perm default|ask|lockdown|yolo` sets how much it asks before running
  commands.
  Some things stay blocked even under yolo.
- `/model NAME` pins a model for the session; `/model auto` drops the pin and
  falls back to the configured default. The router picks the *mode*, never the
  model.
<!---->
## Running it
<!---->
```bash
nix build      # sealed uv2nix venv, wrapper in result/bin/
nix run .# --  # start the agent (Textual interface)
```
<!---->
It reads `~/.son-of-anton/config.yaml` for settings and `~/.son-of-anton/.env`
for secrets.
A local model endpoint is configured like any other
OpenAI-compatible provider:
<!---->
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
<!---->
## Physics runs
<!---->
The physics mode executes code the model wrote.
Two things follow from that,
and both are configured under `physics:`.
<!---->
### Computations run somewhere else, under something else
<!---->
They do **not** run in the agent's own interpreter. That venv deliberately
ships no scientific stack — son-of-anton's dependency scope rule is "only
packages every session uses", and most deployments never run a physics turn —
so `physics.python` names a separate interpreter:
<!---->
```bash
nix build .#physics-runtime
```
<!---->
That gives you ROOT, `analysis_utilities`, NumPy, SciPy, SymPy, matplotlib,
pandas, scikit-learn, xgboost and h5py in one wrapper, built
from the [Analysis-Utilities](https://github.com/ewtodd/Analysis-Utilities)
flake input rather than from this flake's nixpkgs — that project asks
downstream flakes to leave its `nixpkgs` alone (overriding it defeats its
binary cache), and its Python package has to be paired with the interpreter it
was built against. Point `physics.python` at `.../bin/python3`, or set
`SON_OF_ANTON_PHYSICS_PYTHON`. With neither set, computations fall back to the
agent's own interpreter and are limited to the standard library.
<!---->
The `execute_python` tool description is generated by probing that interpreter,
so it advertises what is actually importable.
Where a library carries house
conventions the agent is also told how to use it — a model that merely knows
`analysis_utilities` is importable still writes numpy and matplotlib, because
that is what it has seen.
`physics.runtime_notes` appends your own.
<!---->
### The sandbox is a sandbox
<!---->
Model-authored code runs under [bubblewrap](https://github.com/containers/bubblewrap):
no network, no `$HOME`, a cleared environment (no API keys), fresh
PID/IPC/UTS/cgroup namespaces, and a filesystem holding only the interpreter's
store paths (read-only), the run's own workspace (read-write) and whatever
`physics.data_dirs` declares (read-only).
With `bwrap` missing it **fails
closed** — `physics.sandbox: "off"` is the explicit opt-in to running
unconfined.
<!---->
```yaml
physics:
  model: deepseek-v4-flash          # the Research Manager: strategy, verification
  coder_model: qwen3.8-27b          # whoever is writing code — see below
  python: /nix/store/…-son-of-anton-physics-runtime/bin/python3
  sandbox: bwrap                    # auto | bwrap | off
  data_dirs: [~/lab-data]           # mounted read-only into every computation
  workspace_root: ~/runs            # each run gets a fresh git-versioned subdir
  critique_every_n: 1               # review each iteration from outside
  mcp:                              # lookup tools, per role
    server: oracle                  # an entry in the top-level mcp_servers
    roles:
      manager:  [arxiv, context7]   # strategy wants the literature
      subagent: [context7]          # writing a script wants the API
```
<!---->
### The critic

The Autophysicist is one agent that decides what to investigate, judges its own
sub-agents and decides what is true — and its own prompt names that as the weak
point. "Nothing is reliable until independently verified" is a norm with nothing
enforcing it, and in practice iterations end with a confident plan and an empty
permanent memory.

So each iteration is reviewed from outside: one prompt, one answer, no tools, no
verdict that gates anything. The critique goes into the next iteration's context
and the Manager does what it likes with it; every one is kept in
`CRITIQUE_LOG.md`. `physics.critique_every_n` sets the interval (0 = off).

One call per iteration is a rounding error against a Manager that spends five
rounds and several sub-agent dispatches, which makes this the one place a slow,
more knowledgeable model earns its latency:

```yaml
physics:
  agent_models:
    critic: deepseek-v4-flash-local
```

### Two models, split by role
<!---->
There are two jobs here, not two modes.
**Reasoning**: read the state, decide
strategy, judge what came back, decide what counts as verified.
**Coding**:
take one prompt and write one self-contained script.
<!---->
`physics.coder_model` routes the coding job to a second model, in both modes —
the Autophysicist's `execute_code` sub-agents, and the pipeline's computer
agent.
Everything else stays on `physics.model`, including a sub-agent
dispatched *without* `execute_code` (derive this, find the error in that, argue
the other side): that is the reasoning job wearing a sub-agent's hat, and
routing it to a coding model to save latency gives up what it was dispatched
for.
<!---->
Code is also where the volume is: one call per script, plus up to three more
each time a script fails. So if you have a fast strong coding model and a
slower one with better physics, this is where the first pays for itself and the
second is not missed.
<!---->
### Lookups

`physics.mcp.roles` maps an agent role to the tools it may call, and the split
is the point. The Manager decides strategy and wants the literature; a sub-agent
writing one script wants the API signature it is about to get wrong, and nothing
else. Handing every role everything is how an agent with a fifteen-call budget
ends up browsing instead of working.

An entry matches a whole tool name (`arxiv-get_abstract`) or a server, matching
everything it prefixes (`context7`). A role with no entry gets no tools, and
`roles: {}` turns lookups off entirely. Enforcement is role-scoped, so a
sub-agent asking for a paper is refused rather than quietly served.

Sub-agents get lookups on their retries too — that is precisely when the docs
are worth reading, since the failure is usually an API that does not exist. The
tool loop is lookups only, with no exit tool: the turn ends when the sub-agent
stops calling tools and answers, exactly as it did before, so the "one Python
code block" contract is unchanged.

An unreachable endpoint degrades to no lookups rather than failing the run.

These are called by the agent process, so they are unaffected by the sandbox —
computations still have no network.
<!---->
### Writing a problem spec
<!---->
A spec is a `problem.yaml`: the task text, the `data:` paths to expose, and the
numeric `checks` that score `RESULTS.txt`.
`son-of-anton problem create`
writes one from a dataset and a one-line goal:
<!---->
```bash
son-of-anton problem create \
    --data ~/lab-data/run42 \
    --goal "measure the half-life; any fitting method is fine" \
    --truth reference_results.txt \
    -o problems/run42/problem.yaml
```
<!---->
The probe does the mechanical part deterministically — ROOT trees, branches and
entry counts, `.npy` shapes, CSV headers — and runs inside the same sandbox the
computations will, so what it sees is what the agent will see.
The model gets
only that summary and your goal, and returns the task text; with `--truth` it
does not choose the expected values either.
A spec whose checks score a key the
task never asks for is rejected.
`--no-llm` renders the whole thing from the
probe plus `--truth`.
<!---->
### Running a problem spec
<!---->
```bash
son-of-anton problem run problems/run42/problem.yaml --max-iterations 5
```
<!---->
It prints `ANSWER.md` and `FORMAL_EVAL.md` when it finishes.
`--workspace DIR`
continues an existing run rather than starting a fresh one.
<!---->
**Set `--max-iterations` on a first run.** Physics mode has no wall-clock or
cost gate, so it is the only
ceiling, and the default is 50 iterations of up to fifteen tool calls each.

**Raise `--script-timeout` for large data.** One model-authored script gets 60 s
by default, which came from a scaffold built for symbolic work. Reading a few
hundred thousand waveforms out of a multi-GB file and training on them is
legitimate work that does not fit in a minute, and the agent reads a timeout as
"wrong approach" — so it retries something smaller instead of the thing that
would have worked.

Both are settable declaratively as `physics.max_iterations` and
`physics.script_timeout`.
<!---->
The same thing is reachable from a chat turn — `/mode physics`, then the path —
which is what the gateway does with a Signal message.
Both go through the same
runner.
Either way the run copies the spec into its workspace, which is where
the evaluator reads it from and where `resume` picks it up.
<!---->
Hand it plain prose instead of a path and it still runs; there is just nothing
to score it against, and `FORMAL_EVAL.md` says so rather than claiming a pass.
A path that *looks* like a spec but cannot be read warns rather than silently
becoming the problem statement.
<!---->
## One service per account
<!---->
The NixOS module runs a separate gateway for each account, as a system service,
so they start at boot without anyone logging in.
Each one runs *as* that
account with `SON_OF_ANTON_HOME=~/.son-of-anton`.
<!---->
Sharing the home is what makes this useful.
The service and your own
`son-of-anton` in a terminal end up on one `state.db`.
A conversation you start
on Signal is sitting there in `/sessions` when you open the CLI, and you can
pick it up.
There is nothing to sync because there is only one copy.
<!---->
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
<!---->
`managedAccount = true` matters. Without it the module creates and chmods the
directories it manages, which against a real home means `2770` on
`/home/e-work`. That makes sshd's `StrictModes` stop accepting your
`authorized_keys`, and it hands everyone else in your primary group write
access to your whole home. With it, the module only touches `~/.son-of-anton`.
<!---->
### Everyone shares one Signal number
<!---->
All the services talk to one signal-cli daemon and all of them see every
message, because signal-cli broadcasts events over SSE. What separates them is
the group: each service answers exactly one Signal group and drops everything
else before it does any work.
<!---->
So set up one group per service, put yourself and the bot in it, and give each
service its group id:
<!---->
```bash
curl -s -X POST http://YOUR-SIGNAL-HOST:7583/api/v1/rpc \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"listGroups","params":{},"id":"1"}' \
  | jq -r '.result[] | "\(.name)\t\(.id)"'
```
<!---->
Put the id in that instance's env file as `SIGNAL_GROUP_ALLOWED_USERS`, along
with `SIGNAL_ALLOWED_USERS`, the list of people allowed to talk to it. That
list is checked on group messages too, so it has to include you.
<!---->
Direct messages are dropped by default (`SIGNAL_DM_MODE=ignore`). A DM has no
group id, so there is no way to tell which service should answer it. Leave it
on and one DM gets you one reply from every running service.
<!---->
If your signal-cli runs on a different machine, note that it opens attachment
paths on **its own** filesystem, so a file the gateway just wrote is not there.
The gateway retries those sends with the file inlined as a data URI, which is
capped at 16 MB.
<!---->
### A group with other people in it
<!---->
You can run an instance for a shared group, like household planning with a
partner. Give it its own service account rather than a personal one:
<!---->
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
  settings.router.modes = [ "standard" ];   # no physics here
};
```
<!---->
Its env file lists everyone in the group under `SIGNAL_ALLOWED_USERS`. Anyone
not listed gets dropped even if they are in the group.
<!---->
Use a service account here, not your own. The agent acts for whoever is
talking, so running it as `e-play` would hand the other people in that group
`e-play`'s home directory, ssh keys, and git identity. Its working directory is
the only thing it can reach.
<!---->
`extraPackages` is per-instance and lands on that service's `PATH`, which is
how you give one instance tools the others don't have.
<!---->
A note on pandoc and PDFs: a plain `pandoc in.md -o out.pdf` fails under a
systemd service. pandoc's typst template writes `font: <mainfont>` and typst
rejects an empty font list, and the service has no fontconfig so typst finds no
fonts at all. Wrap it:
<!---->
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
<!---->
### Home Manager
<!---->
There is also a Home Manager module, if you would rather run the gateway as a
user service. Enable linger or it stops when you log out.
<!---->
```nix
imports = [ son-of-anton.homeManagerModules.default ];
services.son-of-anton = {
  enable = true;
  gateway.enable = true;
  environmentFiles = [ config.age.secrets."son-of-anton-env".path ];
};
users.users.YOUR-ACCOUNT.linger = true;
```
<!---->
## What `settings` does to config.yaml
<!---->
Son of Anton writes `config.yaml` at runtime too — `son-of-anton config set`,
the settings panes, `/model` — so activation merges into it rather than
replacing it. The merge is three-way: it records what it wrote in
`~/.son-of-anton/.nix-managed.json`, so the next activation can tell a key you
have *dropped from Nix* from a key something wrote at runtime. Dropping a
setting removes it from disk; runtime keys are never touched. Removals are
named in the activation log.
<!---->
The first activation after that state file appears cannot attribute anything to
Nix, so it removes nothing and instead lists the keys on disk that Nix does not
declare. If they are leftovers from an older generation rather than runtime
settings, `pruneUnmanagedSettings = true` for one rebuild clears them. Leave it
on if you want `config.yaml` purely declarative and are willing to lose runtime
edits on every rebuild.
<!---->
## Shell completions
<!---->
The Nix package installs bash, zsh and fish completions — generated at build
time by walking the live command tree, so they cannot go stale against the CLI
they complete. On NixOS they are picked up from the package automatically.
<!---->
Outside Nix, or to regenerate after a local change:
<!---->
```bash
son-of-anton completion bash > ~/.local/share/bash-completion/completions/son-of-anton
son-of-anton completion zsh  > ~/.zsh/completions/_son-of-anton
son-of-anton completion fish > ~/.config/fish/completions/son-of-anton.fish
```
<!---->
## No installation commands
<!---->
This is not an imperative program. There is no `setup`, `update`, `uninstall`,
`login`/`logout`, `doctor`, or `gateway install|start|stop`: the deployment is
declared by the NixOS or Home Manager module, `systemctl` runs the service, and
that module's `settings` owns `config.yaml`. `gateway` has two actions, `run`
and `status`.
<!---->
(`mcp install` and `skills install` stay — those add content the agent manages
at runtime, not the install itself.)
<!---->
## Commands
<!---->
| Command | What it does |
|---|---|
| `/mode auto\|standard\|physics` | pin the session's agent mode |
| `/model auto\|NAME` | turn routing back on, or pin a model |
| `/perm default\|ask\|lockdown\|yolo` | set the permission mode (shift+tab cycles it for the session) |
| `/commit` | review the uncommitted diff, write a message in the repo's style, commit |
| `/sessions` | list sessions, including ones started on Signal |
| `/q`, `:q`, `/exit` | quit the CLI |
<!---->
`/commit` hands the work to the agent as an ordinary turn, so it reads the diff
with the tools it already has and commits under your normal approvals. By
default it commits with whatever identity the repository or your global git
config already sets. To author commits under a dedicated account instead —
while keeping *you* as the committer, so the log reads "authored by the
account, committed by you" — set both:

```yaml
git:
  author_name: some-bot
  author_email: 12345+some-bot@users.noreply.github.com
```
<!---->
On the command line, `son-of-anton problem create` builds a physics problem spec
and `son-of-anton problem run` runs one; `son-of-anton completion <shell>` prints
a completion script.
<!---->
`/help` lists the rest.
<!---->
## Development
<!---->
```bash
nix flake check   # package, both modules, venv import smoke
nix develop       # python dev shell with the editable venv
```
<!---->
Run the test suite through the dev shell (`nix develop -c scripts/run_tests.sh`);
it needs `SON_OF_ANTON_PYTHON` from the shell hook.
<!---->
One trap worth knowing if you edit and re-run in a loop: an edit that keeps a
file the same size, made within the same second as the last one, can leave
CPython using the old `.pyc`, because invalidation is on mtime plus size. Clear
`__pycache__` between runs.
<!---->
## License
<!---->
MIT.
Hermes Agent and PhysicsIntern are the work of Nous Research and
HuggingFace; see the upstream repositories for their contributor lists.
opencode, whose interface design the TUI follows, is the work of SST and is
also MIT licensed.
