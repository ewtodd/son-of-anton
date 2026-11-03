# Phase 4a: subagents (daemon side)

Goal: the `task` tool runs a subagent as a child session, opencode v1.19.5's
model (SPEC section 9). Phase 4b adds the TUI's subagent view.

Read first: `SPEC.md` sections 7 ("Prompt composition") and 9, `phases/02a-server.md`,
and the code. The reference is opencode v1.19.5, cloned read-only at
`~/.cache/claude-ref/opencode-v1.19.5` (oc = `packages/opencode/src/`):
`oc/tool/task.ts`, `oc/tool/task.txt`, `oc/tool/registry.ts:265-327`,
`oc/agent/agent.ts`, `oc/agent/prompt/explore.txt`,
`oc/agent/subagent-permissions.ts`. Where this file and opencode differ, this
file wins.

## Already written (do not modify)

`internal/subagent/subagent_test.go`, `internal/agent/concurrent_test.go`,
the last test in `internal/prompt/prompt_test.go`,
`internal/store/children_store_test.go`, `internal/server/task_server_test.go`;
`fakellm`: `Turn.When` (a turn only a request containing that text takes) and
`ToolCall.ArgumentsFunc`.

## package subagent (new)

```go
type Type struct { Name, Description, Prompt string; Tools []string } // Tools nil: every tool the child may have
func Builtin() []Type                                    // explore, general
func Lookup(types []Type, name string) (Type, bool)
func ChildMay(tool string) bool                          // false for task, memory, clarify, todo
func Description(types []Type, background bool) string   // the task tool's description
func Render(id, state, summary, text string) string      // opencode's renderOutput
type Request struct { Description, Prompt, TaskID string; Type Type; Background bool; CallID string }
type Result struct { ID, State, Summary, Text string }    // State: completed | running | error
type Spawner func(ctx context.Context, env *tools.Env, req Request) (Result, error)
func Tool(types []Type, background bool, spawn Spawner) tools.Tool
```

**Types.**
- `explore`: Description verbatim from `oc/agent/agent.ts:196-218` ("Fast agent
  specialized for exploring codebases. Use this when you need to quickly find
  files by patterns (eg. "src/components/**/*.tsx"), search code for keywords
  (eg. "API endpoints"), or answer questions about the codebase (eg. "how do
  API endpoints work?"). When calling this agent, specify the desired
  thoroughness level: "quick" for basic searches, "medium" for moderate
  exploration, or "very thorough" for comprehensive analysis across multiple
  locations and naming conventions."). Tools `terminal`, `read_file`,
  `search_files`. Prompt: `oc/agent/prompt/explore.txt` with its tool names
  mapped (Glob, broad file pattern matching → `search_files` with target
  "files"; Grep → `search_files`; Read → `read_file`; Bash → `terminal`), the
  rest verbatim. It must begin "You are a file search specialist."
- `general`: Description verbatim ("General-purpose agent for researching
  complex questions and executing multi-step tasks. Use this agent to execute
  multiple units of work in parallel."). Tools nil. opencode gives it no
  prompt; soa-d's is: "You are a subagent: another agent gave you one task. Do
  it fully and on your own, then reply with one final message that contains
  exactly what the task asked for; that message is all the agent that called
  you will see."
- A child never has `task` (so the depth is 1, as `oc/agent/subagent-permissions.ts`
  makes it), `todo`, `clarify` (opencode denies question to subagents), or
  `memory` (the Python fork blocked memory writes from subagents).

**Description.** `oc/tool/task.txt` verbatim with its tool names mapped (the
Read or Glob tool → `read_file` or `search_files`; the Grep tool →
`search_files`; the Read tool → `read_file`); then, when `background`, a blank
line and BACKGROUND_DESCRIPTION (`oc/tool/task.ts:25-30`); then "\n" and
"Available agent types and the tools they have access to:" with one "- <name>:
<description>" line per type, sorted by name (`oc/tool/registry.ts:265-277`).

**Parameters** (`oc/tool/task.ts:43-62`, descriptions verbatim): `description`,
`prompt`, `subagent_type` (required), `task_id`, and `background` only when
offered. No `command`.

**Render** (`oc/tool/task.ts:64-79`): `<task id="<id>" state="<state>">`, a
`<summary>…</summary>` line when there is a summary, then `<task_result>` (or
`<task_error>` for state error), the text, the closing tag, `</task>`, joined
by newlines. The tool's result is that text (not JSON).

**Errors**, each as a rendered error or plain text result: "Unknown agent
type: <t> is not a valid agent type" (no spawn); the spawner's errors as
rendered results with state error.

The tool implements `Concurrent() bool` (true).

## package agent

A run of consecutive calls in one assistant message to tools that implement
`Concurrent() bool` returning true runs at the same time; other calls run one
at a time, in order. Results are stored in the calls' order. `Stop(false)` and
cancellation reach every running call.

## package prompt

`Options.Identity`: when set, it is layer 1 whatever `Memory` says (a lean
prompt gets it; SOUL.md and the default are not used). Rules still follow
`Memory` (lean: delegated rules only).

## package store

Schema v7: `sessions.agent`. `Session.Agent`; `Children(ctx, parentID)
([]Session, error)` in creation order. Session lists stay top-level.

## server

- Every interactive session's registry gains `task` (`subagent.Tool(subagent.Builtin(),
  true, spawner)`); children's registries never do.
- **Spawning** (foreground): a child session (`Kind` "child", `ParentID` the
  caller, `Source` "task", `Agent` the type, `Title` "<description> (@<type>
  subagent)", title source user so it is never generated; the parent's cwd,
  model, reasoning effort, scope and permission mode). Its prompt is
  `prompt.Build` with `Memory: false` and `Identity` the type's prompt (no
  recall). Its registry: the type's tools, or the parent's builtin tools that
  `ChildMay`. It is a loaded session like any other (viewers may attach); its
  approvals use the parent's approver (they surface in the parent's driver,
  with `child_session`), and it has no questioner. The task call's context is
  the child's: cancelling the parent's turn cancels it.
- Events on the parent: `child.created` (`call_id`, `child`: the session
  object) when the child is made, `child.done` (`child_id`, `status`: done,
  interrupted, or error) when its turn ends.
- The result: completed with the child's final reply; "Subagent failed
  (task_id: <id>): <error>" as an error result when its turn failed; "Task
  cancelled" (state error) when it was stopped.
- **Background**: the call returns at once with state running, summary
  "Background task started" and BACKGROUND_STARTED (`oc/tool/task.ts:31-35`)
  as the text. The child runs on (cancelled with the parent's turn, as
  opencode cancels a session's jobs); when it ends, the parent gets a notice
  (the 2a notice path: a turn now if idle, else queued) whose text is the
  rendered result with summary "Background task completed: <description>" or
  "Background task failed: <description>".
- **task_id**: a child of this session continues: a new user message with the
  prompt on the same child, which keeps its history. If that child is running,
  the prompt is queued on it and the call returns state running, summary
  "Background task updated", text BACKGROUND_UPDATED (`oc/tool/task.ts:36-41`).
  Any other id: "no task <id> in this session" (**fix**: opencode silently
  starts a fresh child).
- A child is never a `session_search` result and is never indexed for
  recall (the Python hid "subagent" sessions the same way): its answer is
  already the parent's tool result. (Added in review.)
- The session object gains `parent_id` and `agent`. New request
  `session.children {session}` → `{children: [session objects]}` in creation
  order. `approval.request` from a child gains `child_session`.

## Not in 4a

The TUI's subagent view (4b), configurable agent types, `subagent_depth`
beyond 1, promoting a running foreground task to background.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 4a
(subagents)`, body listing what changed, ending with the `Model:` line.
