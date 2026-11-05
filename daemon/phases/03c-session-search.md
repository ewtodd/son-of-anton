# Phase 3c: session_search and the wall

Goal: the agent can find and read past conversations (`session_search`), by
words and, with recall on, by meaning; and a common caller can never reach a
gateway or chat conversation by any path.

Read first: `SPEC.md` sections 4 (invariant 6, the wall) and 11, the repo's
`AGENTS.md` "Memory and recall", `phases/03b-memory-recall.md`, and the code.
References are into `../tools/session_search_tool.py` (SS) and
`../agent/rag.py` (RAG). Where this file and the Python differ, this file wins.

## Already written (do not modify)

`internal/store/search_store_test.go`, `internal/tools/session_search_test.go`,
`internal/recall/messages_test.go`, `internal/server/search_server_test.go`.

## The wall

Sessions gain `Scope` (`common` | `gateway`, default `common`) and `Chat` (a
chat stamp such as `signal:<id>`, default empty). Until phase 5 every soa-d
session is common with no chat; the rule is built now so it holds when Signal
arrives. RAG:2062-2119:

```go
func Readable(caller string, s store.Session) bool
```

A `gateway` caller reads everything. Any other caller (common, or unknown,
which counts as the terminal) reads only sessions with scope `common` and no
chat. Every shape of `session_search` applies it, and a walled session reads
exactly like a missing one.

## package store

Schema v6: `sessions.scope` (default 'common') and `sessions.chat`.

```go
type MessageQuery struct {
    Query   string   // an FTS5 expression
    Caller  string   // the wall: rows of sessions the caller cannot read never match
    Roles   []string // e.g. user, assistant (and tool when asked)
    Sort    string   // "" (relevance), "newest", "oldest"
    Exclude string   // a session id to leave out (the caller's own)
    Limit   int
}
// Hit gains Role and TS.
func (s *Store) SearchMessages(ctx context.Context, q MessageQuery) ([]Hit, error)
func (s *Store) ListReadable(ctx context.Context, caller string, kinds []string, limit int) ([]Summary, error)
```

- The wall, the roles and `Exclude` are conditions of the SQL query itself
  (joined against `sessions`), never a filter after a `LIMIT`: walled rows
  must not crowd out readable ones.
- An FTS5 syntax error retries once with every term double-quoted (inner
  quotes doubled), so a stray quote never fails a search.
- `ListReadable` is `ListSessions` with the wall.

## package recall

```go
type MessageHit struct { SessionID string; Seq int64; Text string; Score float64 }
func (x *Index) IndexMessages(ctx context.Context, sessionID, scope string, rows []store.Message) error
func (x *Index) SearchMessages(ctx context.Context, scope, query string, k int) ([]MessageHit, error)
```

- Indexed: user rows and assistant rows without tool calls, their `Content`
  only (never reasoning, tool calls or tool output; RAG:555-570), one item per
  row chunked like notes, stamped with the session's scope, keyed by session
  and seq so a row is embedded once. `kind` "message".
- The facts-and-notes refresh never prunes message items, and `Recall`
  never returns them (they are for `session_search` only).
- `SearchMessages`: cosine against the message items the scope can see
  (common: common only; gateway: all), at least `MinScore`, at most `k`, best
  first. The embedder down: an error (the tool then searches by words only).

## package tools: `session_search`

Appended to `Builtin()` after `memory`. `Env` gains `Store *store.Store`,
`SessionID string` (the caller's session) and `SearchMessages MessageSearch`
(nil when recall is off), with

```go
type MessageHit struct { SessionID string; Seq int64; Text string; Score float64 }
type MessageSearch func(ctx context.Context, query string, k int) []MessageHit
```

The caller's scope is `Env.MemoryScope`. No `Store`: an error result.

Schema (adapted from SS:1150-1221; no `profile`, `sources` or links):

- description: "Search or read past son-of-anton conversations (full-text
  search over the session store, plus meaning-based matching when recall is
  enabled; returns the real messages). It only knows what was said here
  before: when the user names a live source (a URL, file, contact, account or
  system), check that source first, and don't conclude something doesn't exist
  from this search alone.\n\nShapes:\n- query: find sessions. The best match
  comes with its opening and closing messages and the messages around the hit;
  the others with just the matching message.\n- session_id +
  around_message_id: read a window around one message. To scroll, pass the
  window's last (forward) or first (backward) message id.\n- session_id alone:
  read a whole session (the first 20 and last 10 messages if long).\n- no
  arguments: list recent sessions.\n\nQuery words are ANDed; use OR, \"quoted
  phrases\", NOT and prefix* as needed."
- properties: `query` ("Words or an FTS5 expression to search for. Omit to
  list recent sessions."), `limit` integer ("Sessions to return when searching
  (default 3, max 10)."), `sort` enum newest/oldest ("'newest' for
  where-did-we-leave-off questions, 'oldest' for how-did-it-start ones; omit to
  rank by relevance."), `detail` enum adaptive/full ("'full' gives every result
  its context; the default 'adaptive' only the top one."), `session_id`
  ("Session to read, from a search result."), `around_message_id` integer
  ("Message id to center the window on, e.g. a result's match seq."), `window`
  integer ("Messages on each side of the anchor (1-20, default 5)."),
  `role_filter` ("Comma-separated roles to search; default 'user,assistant'.
  Add 'tool' to include tool output."). None required.

Shapes (precedence window > read > discover > browse), every one behind the
wall and leaving out the caller's own session:

- **browse** (no arguments): `{"success": true, "mode": "browse", "results":
  [{session_id, title, kind, started_at, last_active, message_count,
  preview}], "count"}`, newest activity first, at most 20.
- **discover** (`query`): text matches (`SearchMessages` with the caller, the
  roles from `role_filter`, `sort`, `Exclude` the caller's session), grouped by
  session in the order of each session's best hit; then, when
  `SearchMessages` is set, meaning matches for sessions not already present
  that the caller can read (re-checked with `Readable`), in score order; at most
  `limit` sessions (default 3, max 10). Each result: `{session_id, title,
  started_at, last_active, match: {seq, role, content}}`; the first (or all,
  with `detail` full) adds `context: {opening, around, closing}`: the first 3
  and last 3 messages and 5 on each side of the hit (fewer where they would
  repeat). `{"success": true, "mode": "discover", "query", "results", "count"}`.
- **read** (`session_id`): `{"success": true, "mode": "read", "session_id",
  "session_meta": {title, kind, model, started_at}, "message_count",
  "truncated", "messages"}`: every message up to 30, else the first 20 and the
  last 10.
- **window** (`session_id` + `around_message_id`): `{"success": true, "mode":
  "window", "session_id", "anchor_seq", "messages"}`, `window` (1-20, default
  5) on each side; an anchor that is not a row of the session is an error.
- A message is `{seq, role, content, ts}`; content is cut at 4000 characters
  (1200 for opening/closing); an assistant row with tool calls ends with
  "[called <name>, <name>]". Tool rows appear only when `role_filter` includes
  `tool`.
- A walled or missing session: `{"success": false, "error": "session_id not
  found: <id>"}`, identical in every shape.

### What a result owes its reader (2026-11-05)

A chat session has only these results to go on, and it guessed a repo path,
called a live session interrupted, read a tool's cut as a reply that stopped
mid-sentence, and repeated one read six times. So:

- Every shape gives `cwd`, `kind` and `state`; browse and discover on each
  result, read and window in `session_meta`. `state` is what `relay list`
  reports (running, idle, queued, picked up) and comes from
  `Env.SessionState`; with none, it is left out. Browse lists workers too.
- A cut message says so: its text ends `[cut: showing N of M characters; for the
  rest pass around_message_id S, content_offset K]`. `content_offset` with
  `around_message_id` reads that message alone from character K.
- `session_id` + `query` (mode `session_query`) searches that session and gives
  `matches`, `anchor_seq` and the window around the best one.
- `session_id` alone refuses `window` and `detail: full` instead of ignoring
  them, and a read that leaves messages out says which, in `omitted`.

## server

- Sessions run with `Env.Store`, `Env.SessionID` and, when recall is on,
  `Env.SearchMessages` (`SearchMessages` with the session's scope; nil on
  error).
- After each turn ends (any status), the session's rows not yet indexed are
  passed to `IndexMessages` in the background; at startup, a background
  backfill indexes every session's rows, newest sessions first. Indexing never
  delays a turn.

## Not in 3c

Chat sessions themselves (phase 5); per-chat isolation between gateway chats;
session links in results.

## README

Status: drop `session_search` from "Not in the daemon yet".

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 3c
(session_search, the wall)`, body listing what changed, ending with the
`Model:` line.
