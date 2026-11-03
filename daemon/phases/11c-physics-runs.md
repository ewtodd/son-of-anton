# Phase 11c: physics runs (worker, manager, scoring)

Goal: `son-of-anton problem run SPEC` works a physics problem: a **worker**
session does the analysis in the sandbox of 11b; when it stops, a
**manager** session reviews the run directory and either accepts or sends it
back with feedback; at the end the run is scored on `RESULTS.txt` against
the spec's checks. The owner wants the manager's end-of-session feedback
(not yet validated: the run records make it possible to judge later).

Why this shape (from the Python harness's runs, which never scored): the
model must run code itself and see the output; it must know how it is
scored; nothing it records should go unchecked; the expected values must
never reach it.

Read first: `daemon/phases/11b-physics-sandbox.md` and what 11b built;
`internal/server/relay.go` and `live.go` (how a worker session is driven
with nobody attached, and the turn-end hook a `workerLink` uses);
`internal/server/server.go` (createAs, runner); `internal/store`
(migrations); `cmd/son-of-anton/main.go`.

## The spec file

TOML: `name` (slug), `problem` (text), `data` (list of paths), `[[checks]]`
(`id`, `key`, `expected`, `tolerance`). A run refuses a spec whose name is
not a slug, whose problem is empty, whose data paths do not exist or are not
under `physics.data_dirs`, that has no checks, a check without a numeric
`expected` or a positive `tolerance`, or a key that does not appear in the
problem text. `answer_template` and `checker` (Python-era fields) are
refused with a message saying they are not supported.

## The run

- **Run directory**: `<physics.workspace_root, else <home>/workspaces>/<YYYYmmdd_HHMMSS>_<name>_<model slug>/`,
  a git repository with a repo-local identity (the instance's
  `git.author_name`/`author_email`, else `son-of-anton
  <son-of-anton@localhost>`), a `.gitignore` (`.home/`, `__pycache__/`,
  `*.root`, `*.npy`, `*.npz`, `*.h5`, `*.bin`, `*.pkl`), `PROBLEM.md` (the
  problem text and the list of keys). **The spec itself, with the expected
  values, is never in the run directory**: it is kept at
  `<home>/physics/<run id>.toml`, and scoring reads it from there. The run
  id is the directory's name.
- **Worker**: a session of kind `physics` (agent `worker`), cwd the run
  directory, sandboxed (11b policy: run directory, the spec's data, the
  runtime of `physics.python`), on `physics.model` (else `model.default`),
  with the system prompt below. Its first message is `PROBLEM.md`'s text
  followed by `Begin.` A **work session** is one turn: it ends when the
  worker replies without a tool call. The turn's tool-step limit is raised
  for physics (`physics.max_steps`, default 400).
- **After each work session**: the controller writes
  `logs/session-N.md` (every tool call of the session: the command or the
  tool and its main argument, exit code, duration, and the first 30 and last
  15 lines of its output, at most 4,000 characters per call), checks
  `RESULTS.txt`'s format (below), commits (`session N: <first line of the
  worker's closing message>`), and starts a review.
- **Manager**: one session of kind `physics` (agent `manager`) per run,
  `physics.manager_model` (else the worker's model), the system prompt
  below, tools `read_file` and `search_files` confined read-only to the run
  directory, and `review` (`verdict`: `accept` | `continue`, `feedback`:
  string). Each review is one turn, with the message below. A review that
  ends without calling `review` gets one reminder (`Call review with your
  verdict.`); if it still does not, the verdict is `continue` and its last
  message is the feedback. Each review is appended to `REVIEWS.md` and
  committed (`review N: accept|continue`).
- **Accept**, or the last work session reviewed: the run is scored.
  **Continue**: the feedback goes to the worker as its next message (form
  below), in the same session, and the next work session starts.
- **Limits** (config under `[physics]`, flags override): `max_sessions`
  (default 8), `session_tokens` (default 400,000), `total_tokens` (default
  3,000,000), `hours` (default 8). Tokens are prompt tokens not served from
  cache plus completion tokens, worker and manager together. At 90% of a
  session's tokens the worker gets the budget message as a steer, the way
  `turn.send` in `now` mode delivers one (its running turn stops after the
  current tools, and the next turn starts with the message); that next turn
  still belongs to the same work session. At 125% the turn is stopped with
  its running commands allowed to finish (`Session.Stop(true)`) and the work
  session ends. When `total_tokens` or `hours` runs out,
  the worker gets the budget message, the current turn ends as above, and the
  run is scored without another review.
- **Scoring**: `RESULTS.txt` lines `key = value`, value a plain number
  (whitespace trimmed; anything else on the line makes it unparseable), last
  value wins, `#` comment lines ignored. A check passes when
  `|value - expected| <= tolerance`. The run passes when every check passes.
  `FORMAL_EVAL.md` (status, n/m, and per check the key, expected, tolerance,
  value or why it has none) is written into the run directory and committed
  (`formal evaluation: passed|failed`).
- **The format check** (given to the manager and, with the feedback, to the
  worker): `RESULTS.txt: every key present, each a plain number.` or
  `RESULTS.txt: missing.` or `RESULTS.txt: missing keys: a, b; not plain numbers: c ("1.2 ± 0.1").`
  It never says whether a value is right.
- **Record**: `EVENT_LOG.jsonl` in the run directory: one line per model
  request (`kind: "llm_call"`, `ts`, `agent`, `session`, `model`,
  `served_model`, `input_tokens`, `cached_tokens`, `output_tokens`,
  `duration_s`, `stop_reason`) and per tool call (`kind: "tool_call"`, `ts`,
  `agent`, `session`, `tool`, `exit_code` when a command, `duration_s`), and
  `config.json` (the spec's name and data, the models, the limits in force).
  Both sessions' full transcripts stay in the store.
- **State**: a `physics_runs` table (migration v16): id, spec path, run
  directory, worker and manager session ids, state (`working`, `reviewing`,
  `passed`, `failed`, `stopped`, `interrupted`), sessions done, tokens used,
  models, started, ended. One run at a time per instance: starting a second
  is refused, naming the running one. A daemon that stops with a run going
  marks it `interrupted`; `resume` continues it (a work session continues
  with `The run was interrupted; carry on where you left off.`; a review is
  repeated).

## The commands

`son-of-anton problem run SPEC [--model M] [--manager-model M]
[--max-sessions N] [--session-tokens N] [--total-tokens N] [--hours H]
[--wait]` prints the run id and directory and returns; with `--wait` it
follows the run (one line per event: work session started and ended with its
tokens, review verdict, scoring result) and exits 0 when it passed, 1 failed,
3 stopped or interrupted. `problem list`, `problem watch ID` (as `--wait`),
`problem stop ID` (interrupts and scores what is there; state `stopped`),
`problem resume ID`. Daemon methods `physics.run|list|watch|stop|resume`.
Usage errors exit 2. A physics session can be watched in the TUI but not
driven: attaching to one as driver is refused, and the TUI attaches as
viewer. Physics sessions are not in the TUI's session lists.

## The prompts (exact; `{…}` filled in)

Worker system prompt:

```
You are working on a physics analysis problem on your own. Nobody can answer questions while you work. When you stop, a reviewer reads your run directory and either accepts the work or sends you back with feedback.

Your run directory is {RUN_DIR}. Every command you run starts there, inside a sandbox: there is no network, the data is mounted read-only at {DATA}, you can write only in the run directory, and your home is {RUN_DIR}/.home.

Installed: python3 with ROOT (PyROOT and RDataFrame), uproot, awkward, numpy, scipy, pandas, matplotlib, scikit-learn, xgboost, h5py and Analysis-Utilities; root and rootls; g++ and make. Compile against ROOT with: g++ -O2 prog.cpp $(root-config --cflags --libs) -o prog. A command is stopped after its timeout (180 s unless you give one, 600 s at most): run anything longer with the terminal's background mode and check on it. Try a full pass over the data on a small slice first.

You are scored on RESULTS.txt in the run directory and on nothing else: one line per key, key = value, the value a plain number with no units, no uncertainty and nothing else on the line. The keys are: {KEYS}. Write the file as soon as you have values, and update it as they improve.

How to work:
- Look before you build. Print a few entries, the branch types and shapes, and check every assumption (polarity, units, sizes, what a field means) against printed output.
- Every number you report must come from output you printed in this run. Record nothing as known that you have not seen.
- Keep scripts as files in the run directory rather than long one-off commands, so they can be rerun and checked.
- Keep NOTES.md in the run directory: what you have established and the command that showed it, what you decided and why, and what is left. Older parts of this conversation may be summarized away; NOTES.md is how you pick up again.
- When you are done, or cannot get further, stop with a short message: what you found, which files hold the evidence, and what is uncertain.
```

Manager system prompt:

```
You review a physics analysis. Another agent works on the problem below in {RUN_DIR}, and you review each stretch of its work when it stops. You can read the run directory but cannot change it or run anything.

Each review gives you the agent's closing message, a check of RESULTS.txt's format, and logs/session-N.md: every command the agent ran in that stretch, with its exit code and the start and end of its output.

The work is finished when:
- RESULTS.txt has every key ({KEYS}), each a plain number;
- each value comes from output the run actually shows: you can point to the script and the printed result behind it;
- the method follows the problem statement wherever the problem defines it (processing steps, fit models, which data), and the choices it leaves open are stated in NOTES.md and reasonable.

You do not know the correct values: do not guess them or steer toward a number. Judge the evidence and the method. When the work is not finished, say what is wrong and what to do about it, most important first, specific enough to act on without asking you. Do not repeat feedback the agent has already acted on.

End every review by calling review, with the verdict accept or continue and your feedback.

The problem:
{PROBLEM}
```

Review message to the manager:

```
Review of work session {N} of at most {MAX}.

The agent's closing message:
{CLOSING}

{FORMAT_CHECK}

The session's commands are in logs/session-{N}.md.
```

Feedback message to the worker:

```
Reviewer's feedback on work session {N}:

{FEEDBACK}

{FORMAT_CHECK}

This is work session {N+1} of at most {MAX}.
```

with, when `N+1` is `MAX`, the last line followed by ` It is your last: leave RESULTS.txt complete with your best values before you stop.`

Budget message (steered into the worker's turn):

```
This work session's budget is nearly spent. Update NOTES.md and RESULTS.txt with where things stand, then stop.
```

## Tests (contract, before the code)

With fakellm for both sessions (the worker's tool calls use `write_file`
and the sandboxed terminal; skip the terminal parts, saying why, where bwrap
cannot run):

- A run whose worker writes a correct `RESULTS.txt` and whose manager
  accepts: state `passed`, `FORMAL_EVAL.md` with every check passing, the
  commits in order, `--wait` exits 0. The same with a wrong value: `failed`,
  exit 1.
- `continue`: the worker's next request carries the feedback message in its
  exact form, with the format check; the session ids stay the same.
- **No request either session makes ever contains an expected value or a
  tolerance** (checked over every body the fake received), and the run
  directory never contains the spec; the worker's file tools cannot read
  `<home>/physics/`.
- The format check's three forms; a missing `RESULTS.txt` reaches the
  manager as `RESULTS.txt: missing.`
- A manager turn without `review` gets the reminder once, then counts as
  `continue`.
- `max_sessions` reached: scored after the last review. `session_tokens`:
  the steer at 90%, the interrupt at 125% (fake usage numbers). `hours` and
  `total_tokens`: scored without another review.
- `logs/session-N.md` lists each tool call with its exit code and clipped
  output; `EVENT_LOG.jsonl` has the llm_call and tool_call lines.
- Spec validation: each refusal listed above.
- A second run while one is going is refused. A daemon stop marks the run
  `interrupted`; `resume` continues the same worker session with the
  interrupted message.
- Attaching to a physics session as driver is refused; as viewer works.

## Done when

The gates in `daemon/AGENTS.md` pass. One commit: summary `daemon: phase 11c
(physics runs)`, body listing what changed, ending with the `Model:` line.
The owner then runs `yap_A_peak_centroids` on the work instance (phase 11d);
`problem create` is phase 11e.
