# Phase 7c: execute_code and vision_analyze

Goal: the two remaining heavily used tools (execute_code 609 and
vision_analyze 212 calls in the owner's history).

Read first: `SPEC.md` section 8, `phases/07a-web.md`, and the code. The
references are the Python `tools/code_execution_tool.py` (CE) and
`tools/vision_tools.py` (VT). Where this file and the Python differ, this
file wins.

## Already written (do not modify)

`internal/server/code_vision_server_test.go`, `cmd/soa-d/code_test.go`.

## Decisions made without the owner (2026-11-02, night)

- execute_code runs without asking in interactive, worker and child sessions
  in the `default` and `yolo` modes, as the Python's terminal never asked
  for it; `ask` and `lockdown` ask (the script is the subject); `-q`/`-z`
  without `--yolo`, and cron, refuse it (the Python's single_query_mode and
  cron_mode) with `{"status": "error", "error": <why>, "tool_calls_made": 0,
  "duration_seconds": 0}`, the script never started. Every tool call the script makes goes through the same gate as
  a direct call (a dangerous `terminal()` asks the driver).
- A relative vision path resolves against the session's directory (the
  Python used the process's).
- Chat sessions get vision_analyze in a later step, once phase 5b has
  merged (reading the chat's attachments in `cache/signal`).
- One new dependency, `golang.org/x/image` (BMP, TIFF and WebP decoding and
  scaling), pinned per `AGENTS.md`.

## execute_code

`Builtin()` gains `execute_code` after `web_extract`.

- **Schema:** CE:1315-1368 with CE:1289-1312's doc lines for each sandbox
  tool the session has (of `web_search`, `web_extract`, `read_file`,
  `write_file`, `search_files`, `patch`, `terminal`, in that order), with two
  changes: "with its venv's python" becomes "with Python 3", and the doc
  lines match soa-d's own tools (no `cross_profile`; `patch` is find-and-
  replace only). The `code` parameter's description is CE's.
- **Run:** a fresh temp directory holds `son_of_anton_tools.py` (the stub
  module: CE:242-285 and the UDS header CE:412-465 with the helpers
  CE:369-408, generated with the session's sandbox tools and soa-d's
  parameters) and `script.py`; the interpreter runs `script.py` in the
  session's directory (the tool state's cwd) in its own process group, stdin
  closed. Interpreter: `$VIRTUAL_ENV/bin/python`, `$CONDA_PREFIX/bin/python`,
  `$SON_OF_ANTON_PYTHON` (the Nix wrapper sets it), then `python3` on PATH,
  the first that exists; none: `{"status": "error", "error": "execute_code
  needs a Python 3 interpreter and found none"}`. The directory is removed
  afterwards.
- **Environment** (CE:114-215, 756-782): drop any name containing `KEY`,
  `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`,
  `WEBHOOK`, `CREDS`, `BEARER` or `APIKEY` (upper-cased); keep names starting
  `PATH HOME USER LANG LC_ TERM TMPDIR TMP TEMP SHELL LOGNAME XDG_ PYTHONPATH
  VIRTUAL_ENV CONDA`, and `SON_OF_ANTON_HOME`; drop the rest. Then
  `SON_OF_ANTON_RPC_SOCKET`, `SON_OF_ANTON_RPC_TOKEN` (32 random bytes,
  URL-safe base64), `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`,
  `PYTHONUTF8=1`, and `PYTHONPATH` = the temp directory, then any inherited
  `PYTHONPATH`.
- **RPC** (CE:475-617): a unix socket in the temp directory, mode 0600,
  accepting one connection; newline-delimited `{"tool", "args", "token"}`
  requests, each answered with the tool's result string as one JSON line
  (the stub decodes it twice when it is a string). A wrong token is
  "Unauthorized RPC request"; a tool not offered, "Tool '<x>' is not
  available in execute_code. Available: <sorted, comma-separated>"; the 51st
  call, "Tool call limit reached (50). No more tool calls allowed in this
  execution."; `terminal`'s `background` and `notify_on_complete` are
  dropped. Each call runs through the session's own tool, environment and
  gate, as a direct call would.
- **Limits:** 300 s wall clock (SIGTERM to the group, SIGKILL 5 s later);
  stdout keeps its first 40% and last 60% of 50,000 bytes with "\n\n...
  [OUTPUT TRUNCATED - <omitted, thousands separated> bytes omitted out of
  <total> total] ...\n\n" between; stderr keeps its first 10,000 bytes.
- **Result** (CE:981-1020): `{"status": "success"|"error"|"timeout"|
  "interrupted", "output", "exit_code" (-1 unknown), "tool_calls_made",
  "duration_seconds", "stdout_truncated", "stdout_bytes_captured",
  "stdout_bytes_total", "stdout_bytes_omitted"}`, plus `"warning"` when
  truncated. A non-zero exit: status error, `"error"` the stderr or "Script
  exited with code N", and the output `stdout + "\n--- stderr ---\n" +
  stderr`. Exit 0 discards stderr. Timeout: `"error": "Script timed out
  after 300s and was killed."` and "\n\n⏰ " plus that appended to the output.
  Interrupted (the turn stopped): the output ends "\n[execution interrupted
  — user sent a new message]". CE's pre-checks for a `command` argument and
  a non-string or empty `code` keep their messages.

## vision_analyze

`Builtin()` gains `vision_analyze` after `execute_code`.

- **Schema:** VT:912-953 verbatim.
- **Load** (VT:87-151): `data:` URLs with `;base64` (at most 50 MB);
  `http(s)` through phase 7a's private-address-guarded fetch (honouring
  `web.allow_private_urls`), `Accept: image/*,*/*;q=0.8`, the browser
  User-Agent, at most 50 MB; anything else is a local path, `file://`
  stripped, resolved like `read_file`'s paths (the session directory, `~`)
  and checked with the path policy's `CheckRead` (secret files refused).
  Other schemes: VT's "Unrecognized image source scheme…" message.
- **Type by magic bytes** (VT:59-80): PNG, JPEG, GIF and WebP pass through;
  BMP, TIFF and ICO become PNG; SVG goes through `rsvg-convert` when it is on
  PATH (VT's message otherwise); anything else is "source is not a
  recognized image".
- **Region** (VT:372-433): `[x1, y1, x2, y2]` pixels of the original,
  clamped, x2 and y2 exclusive, applied before any scaling, the crop sent as
  PNG; zero area and malformed input keep VT's messages.
- **Size:** a data URL over 20 MB is scaled down (halving each side, at
  least 64 px, PNG stays PNG, others JPEG at quality 85, 70, 50) until it is
  under 5 MB; still over 20 MB is VT's "Image too large…" error.
- **Request:** to `auxiliary.vision` (base_url, key_env, model, timeout;
  default: the session's own endpoint and model), `temperature` 0.1, one
  user message: `[{"type": "text", "text": "Fully describe and explain
  everything about this image, then answer the following question:\n\n<question>"},
  {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}]`.
  The provider gains user messages with content parts for this; nothing else
  uses them.
- **Result** (VT:834-887): `{"success": true, "analysis": "[<note>] <text>"
  or "<text>", "scale_note"}` with `scale_note` only when scaled or cropped
  (VT:436-478's texts); `<think>` blocks stripped. A failure: `{"success":
  false, "error": "Error analyzing image: <why>", "analysis": "There was a
  problem with the request and the image could not be analyzed."}`.

## README

Status: drop execute_code and vision_analyze from anything that says they
are missing (if listed).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (run the Go tests with a
Python 3 on PATH, e.g. `nix shell --inputs-from .. nixpkgs#go nixpkgs#python3`;
add `python3` to the Nix package's check inputs so `nix build` runs these
tests too; update the `vendorHash`), `ruff check .` and `ruff check
--select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole suite),
and `nix flake check`. One commit: summary `daemon: phase 7c (execute_code,
vision_analyze)`, body listing what changed, ending with the `Model:` line.
