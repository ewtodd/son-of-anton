---
name: relay-coding
description: Drive a headless coding session from a chat surface.
version: 1.0.0
author: Ethan Todd
license: MIT
platforms: [linux, macos, windows]
metadata:
  son-of-anton:
    tags: [gateway, signal, discord, slack, sessions, coding, relay]
    category: software-development
---

# Relay Coding

Run real coding work from a chat surface without doing the coding in the chat.

## When to Use

- The user asks a chat surface (Signal, Discord, Slack) for work in a project.
- The work should live in a normal CLI session the TUI can pick up later.
- Not for questions answerable from the conversation, and not for work the
  relay itself can answer without touching a project.

## Prerequisites

- This session is a gateway session; the user is reachable in chat.
- `son-of-anton` is on `PATH`.
- Keep the coder on the default permission mode. Never pass `--yolo`.

## How to Run

1. Resolve the workspace: the project directory the user named, or their
   configured working directory. Change into it first — sessions are grouped
   by workspace (git repo root, else cwd).
2. List that workspace's sessions, most recently active first:

   ```bash
   son-of-anton sessions list --json --here --limit 5
   ```

3. If a session exists, ask before choosing:

   > A session from `<last_active_relative>` exists here — "<title or
   > preview>" (`<message_count>` messages). Continue it, or start a new one?

   Wait for the answer. Do not continue or start on the user's behalf.
4. Start the coder with the terminal tool, in the background with completion
   notification on — a coding turn can run for minutes:

   ```bash
   SON_OF_ANTON_DURABLE_APPROVALS=1 son-of-anton --resume <session_id> "<task>"
   SON_OF_ANTON_DURABLE_APPROVALS=1 son-of-anton "<task>"          # new session
   ```

   `SON_OF_ANTON_DURABLE_APPROVALS=1` is what makes a dangerous-command
   approval reach this chat instead of auto-denying.
5. For a task with quotes, newlines or shell metacharacters, write it to a
   file and use the file transport:

   ```bash
   SON_OF_ANTON_DURABLE_APPROVALS=1 son-of-anton chat --resume <session_id> --query-file /tmp/task.txt
   ```

6. Dangerous commands arrive in this chat automatically, with
   `/approve <id> [session|always]` / `/deny <id>` instructions from the
   gateway. Never approve or deny on the user's behalf.
7. When the completion notification arrives, report what the coder concluded.
   For a follow-up, `--resume` the same session id.

## Quick Reference

| Action | Command |
|---|---|
| Sessions in this workspace | `son-of-anton sessions list --json --here --limit 5` |
| Continue a session | `SON_OF_ANTON_DURABLE_APPROVALS=1 son-of-anton --resume <id> "<task>"` |
| New session | `SON_OF_ANTON_DURABLE_APPROVALS=1 son-of-anton "<task>"` |
| Arbitrary text task | `son-of-anton chat --resume <id> --query-file <path>` |
| Answer an approval | user replies `/approve <id>` or `/deny <id>` |

## Procedure

1. The user names a project and a task.
2. `cd` to the project and run the JSON listing.
3. Present the most recent session (title or preview, when it was last
   active, message count) and ask continue-or-new.
4. Spawn the chosen session as a background terminal command with completion
   notification, setting `SON_OF_ANTON_DURABLE_APPROVALS=1`.
5. Stay out of the work: do not edit files, run builds, or duplicate the
   coder's task in this session.
6. On completion, summarize the coder's answer. Offer to resume the session
   for the next step.

## Pitfalls

- Doing the coding here defeats the point; this session only relays.
- Don't pass `--yolo`, and don't auto-approve the coder's approvals.
- Don't silently pick a session for the user; the continue-or-new choice is
  theirs.
- The JSON listing is already ordered by last activity; `last_active_relative`
  is for display only.
- One coding turn per process: don't start a second coder for the same
  workspace while one is still running.
- `--resume` restores the session's recorded working directory, so a resumed
  task runs where the session started, not where this chat happens to be.

## Verification

- `son-of-anton sessions list --json --here` shows the session with a newer
  `last_active` and a higher `message_count` after the turn.
- The same session is resumable from the TUI: `son-of-anton -c` from that
  directory, or `-c <title>`.
