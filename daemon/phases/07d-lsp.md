# Phase 7d: LSP diagnostics after edits

Goal: after `write_file` or `patch` changes a file, soa-d asks the file's
language server for diagnostics and appends the errors the edit introduced
to the tool's result (SPEC sections 8 and 14), as the Python did.

Read first: `SPEC.md` section 14, and the code (`internal/tools`
writefile.go and patch.go). The reference is the Python `agent/lsp/`
(`manager.py`, `client.py`, `servers.py`, `reporter.py`) and
`tools/file_operations.py:1646-1731, 2175-2225`. Where this file and the
Python differ, this file wins.

## Already written (do not modify)

`internal/server/lsp_server_test.go` (its language server is the test
binary started again, speaking LSP over stdio).

## Decisions made without the owner (2026-11-03, night)

- The built-in servers are those the owner's nixvim enables (and the Nix
  wrapper puts on PATH): basedpyright/pyright, typescript-language-server,
  gopls, rust-analyzer, clangd, bash-language-server, yaml-language-server,
  nixd, texlab, vscode-json-language-server. Others can be added in config.
- The edit's new errors are told from old ones by a multiset of (severity,
  code, source, message), not the Python's line-shifting diff: an error that
  moved with the edit is not new; one more of the same is.

## Config

`[lsp]`: `enabled` (true), `wait_timeout` (seconds to wait for diagnostics,
5), `idle_timeout` (seconds before an unused server is stopped, 600; 0
never). `[lsp.servers.<id>]`: `disabled`, `command` (a list; its first item
must exist), `env` (added to the daemon's environment), and
`initialization_options`.

## Servers

| id | extensions | command (first found on PATH) | root markers |
|---|---|---|---|
| pyright | .py .pyi | basedpyright-langserver, pyright-langserver (`--stdio`) | pyproject.toml setup.py setup.cfg requirements.txt Pipfile pyrightconfig.json |
| typescript | .ts .tsx .js .jsx .mjs .cjs .mts .cts | typescript-language-server --stdio | package.json tsconfig.json |
| gopls | .go | gopls | go.work go.mod |
| rust-analyzer | .rs | rust-analyzer | Cargo.toml |
| clangd | .c .h .cc .cpp .hpp .cxx | clangd --background-index | compile_commands.json compile_flags.txt .clangd |
| bash | .sh .bash | bash-language-server start | |
| yaml | .yaml .yml | yaml-language-server --stdio | |
| nixd | .nix | nixd | flake.nix |
| texlab | .tex .bib | texlab | |
| json | .json .jsonc | vscode-json-language-server --stdio | |

A file is checked only when a server claims its extension, is not disabled,
its command exists, and the file is inside a git worktree (the nearest
ancestor with `.git`, a file or a directory). The server's root is the
nearest ancestor with one of its markers, else the git root. One server
process per (server, root), started on first use and kept until idle.

## Protocol (client.py)

stdio JSON-RPC with `Content-Length` framing; the process in its own group,
stopped with `shutdown`/`exit` then SIGTERM. `initialize` (45 s) with the
root as `rootUri` and workspace folder, client capabilities for
`textDocument/publishDiagnostics` (with version support) and
`textDocument/diagnostic`, position encoding utf-16, and the configured
`initialization_options`; then `initialized`. A file's first touch is
`didOpen` (version 0, the whole text); later ones `didChange` (a full
replacement, version +1); each followed by `didSave`. Diagnostics: the
latest `publishDiagnostics` for the file with a version at least the
current one, or a `textDocument/diagnostic` pull result when the server
offers it, whichever comes first within `wait_timeout`.

## The report

Before the write, the file's current diagnostics (when the server already
has the file open, its last published set; else none). After it, the new
set; the edit's new errors are the ERROR (severity 1) diagnostics beyond
the old ones' count for the same key. When there are any, the tool's JSON
result gains `"lsp_diagnostics"` (reporter.py, exactly):

```
LSP diagnostics introduced by this edit:
<diagnostics file="<abs path, html-escaped with quotes>">
ERROR [<line+1>:<character+1>] <message> [<code>] (<source>)
... and <n> more
</diagnostics>
```

At most 20 lines (then "... and N more"); message 300, code and source 80
characters, each with CR/LF made spaces, control characters dropped, and
`< > &` escaped; ` [code]` and ` (source)` left out when empty; the whole
string cut at 4000 characters with "\n…[truncated]". No new errors, no
server, a server that fails or does not answer in time: no key, and the
write itself is never delayed by more than `wait_timeout` plus 2 s nor
failed. A server that fails to start or times out is not tried again for
that root until the daemon restarts (logged once).

## README

One line: write_file and patch report new errors from the file's language
server.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 7d (LSP
diagnostics)`, body listing what changed, ending with the `Model:` line.
