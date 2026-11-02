# Phase 3b: the memory tool and recall

Goal: the agent can save rules and facts (`memory` tool), every rule change
asks the user, and each turn of a full-memory session carries the facts and
notes relevant to it. Phase 3c adds `session_search` and enforces the wall.

Read first: `SPEC.md` section 11, the repo's `AGENTS.md` "Memory and recall",
`phases/02a-server.md`, and the code. References are into
`../tools/memory_tool.py` (MT), `../tools/write_approval.py` (WA) and
`../agent/rag.py` (RAG). Where this file and the Python differ, this file wins.

## Already written (do not modify)

- Go: `internal/memory/memory_test.go`, `internal/tools/memory_tool_test.go`,
  `internal/recall/recall_test.go`, `internal/agent/recall_test.go`,
  `internal/server/memory_server_test.go`, `cmd/soa-d/recall_cmd_test.go`;
  `fakellm` gains `/v1/embeddings` (`WordVector`, `SetEmbedStatus`, `EmbedCalls`).
- Python: the phase 3b test at the end of `tests/test_tui_daemon.py`.

## The files are shared

Until phase 8 the Python gateway and soa-d use the same home, so soa-d reads
and writes the Python's files exactly: `memories/rules.toml`
(`[[rule]]`: `id`, `text`, optional `why`, `scope`, `source`, `added`, plus
`delegated` and any unknown key, kept), `memories/facts.toml` and
`facts.gateway.toml` (`[[fact]]`: `id`, `text`, `source`, `added`; the file is
the scope). The agent writes `source = "agent"` and `added` as a TOML date.
An empty store is written as `rule = []` / `fact = []`. Every read-modify-write
holds an exclusive `flock` on the sidecar `<file>.lock` (the Python's lock;
MT:281-295), plus an in-process mutex, and writes atomically (temp file, fsync,
rename).

## package memory (new)

```go
const DefaultRulesCharLimit, DefaultFactCharLimit = 6000, 400
type Rule struct { ID, Text, Why, Scope, Source string; Delegated bool }
type Fact struct { ID, Text, Source string }
type Change struct { Kind, Action, ID, Text, Why, Scope string } // Scope "" or "current": the caller's
type Result struct { ID, Kind, Scope, Usage string }             // Usage, rules only: "1,234/6,000 chars"
type Plan struct { Summary, Detail string }                      // "add rule (common)"; text, then "\nwhy: <why>"
type Error struct { Message string; Similar []Fact; IDs []string }
type Store struct {
    RulesCharLimit, FactCharLimit int
    Similar func(scope, text string) []Fact // near-duplicate search; nil means token Jaccard
}
func New(home string) *Store
func EntryID(text string, taken map[string]bool, fallback string) string
func NormalizeScope(s string) (string, bool)
func (s *Store) Check(c Change, caller string) (Plan, error) // validates, writes nothing
func (s *Store) Apply(c Change, caller string) (Result, error)
func (s *Store) Rules() ([]Rule, error)
func (s *Store) Facts(scope string) ([]Fact, error)
```

`caller` is the calling session's scope. Port MT's behavior and its error
messages verbatim (MT:457-596; the list is in the phase notes below):

- Kinds `rule` and `fact`; actions `add`, `replace`, `remove`.
- Rule text and why are whitespace-collapsed; fact text is trimmed; a fact's
  why is dropped.
- Ids: `EntryID` (MT:161-178): up to six `[a-z0-9]+` tokens of the lowercased
  text joined by `-`, stopping before 32 characters; a lone long first word is
  cut at 32; no tokens gives `fallback`; a clash gets `-2`, `-3`, ... across
  the whole file. Replace keeps the id.
- Scopes: `NormalizeScope` maps `common`, `shared`, `cli` (trimmed,
  case-insensitive) to `common` and `gateway` to `gateway`; anything else is
  rejected ("Unknown memory scope '<s>'. Use one of: common, gateway.").
  Rules live in one file with a `scope` key (none means common); facts live
  in the scope's file. Replace and remove look in the named scope, or else in
  the scopes the caller can see (a common caller: common; a gateway caller:
  both).
- Limits: a fact over `FactCharLimit` is refused with the notes/ hint. The
  rules size is the sum over all rules (any scope) of `len("- [id] text
  (why)") + 1`; a change is refused when the new size is over the limit and
  larger than the old one (shrinking is allowed), with the "rules store is
  full" message. Nothing is ever evicted.
- Near-duplicates, on fact add only: `Similar` when set, else a fact of the
  same scope whose token set has Jaccard similarity at least 0.8.
- An unparseable file is never written ("Refusing to write <name>: ...").
- **(fix) The wall in errors.** A common caller may file a gateway entry, but
  no error it gets names gateway content: no `Similar`, no `IDs`, and no
  gateway text or id in `Message` (the Python leaks both; MT:529-535, 559-562).

## package tools: `memory`

Appended to `Builtin()` (after `clarify`). The schema is MT:897-935 verbatim,
except that the description's last sentence is "Files: memories/rules.toml,
facts.toml, facts.gateway.toml under the instance home." (one schema for every
instance). `Env` gains `Memory *memory.Store` (nil: "Memory is not available.
It may be disabled in config or this environment."), `MemoryScope string` and
`MemoryApproval bool` (`memory.write_approval`).

Flow: `Check`; then for a rule, or a fact when `MemoryApproval`, ask
`Gate.Memory(ctx, plan.Summary, plan.Detail)`; then `Apply`. Results:
`{"id", "success": true, "kind", "scope", "usage" (rules), "note": "Saved.
This update is complete — do not repeat it."}`; errors `{"success": false,
"error", "similar": [{id, text}], "ids": [...]}` with the extra keys only when
non-empty. A denial is "Memory write denied by user. The change was not
saved."

## package approval

```go
func (g *Gate) Memory(ctx context.Context, summary, detail string) Verdict
```

Asks the approver whatever the permission mode (yolo included; WA:324-375):
`Request{Kind: "memory", Subject: detail, Reasons: []string{"Save to memory: "
+ summary}, AllowRemember: false}`. `once` or `session` allows; anything else,
or a cancelled context, denies with the message above. No approver: denied
with "Nobody can approve a memory change in this run; it was not saved."
(The Python stages it for `/memory approve`; soa-d has no staging yet.)

## package recall (new)

```go
type Config struct {
    BaseURL, APIKey, Model      string
    TopK                        int
    MinScore                    float64
    DocumentPrompt, QueryPrompt string
    SearchTimeout, FailureCooldown time.Duration
    NotesDir                    string // "" means <home>/notes
    MaxChunkChars               int
}
func ConfigFrom(c *config.Config, home string) (Config, bool) // ok: enabled with a base URL
func Open(home string, cfg Config) (*Index, error)            // the index: <home>/recall.db, 0600
func (x *Index) Close() error
func (x *Index) Recall(ctx context.Context, scope, query string) string
func (x *Index) Similar(ctx context.Context, scope, text string) []memory.Fact
```

- **Indexed:** each fact as one item (label `fact <id>`), and note files
  (`.md`, `.markdown`, `.txt`) under `NotesDir`, whose first directory names the
  scope (`gateway`, else common; `cli` is common), cut like RAG:514-545
  (whitespace normalized; up to 1200 characters is one chunk; longer text in
  1200-character windows ending at the last space after the first 600, with
  200 of overlap; `MaxChunkChars` cuts each chunk), labelled with the file's
  base name. Nothing else (no transcripts, no tool output).
- **Fresh at each recall:** before a search, `Recall` checks the facts files
  and the notes tree and embeds only what changed (by content), dropping what
  vanished. An unparseable facts file leaves its items as they were.
- **Embedding:** `POST <BaseURL>/embeddings` with `{"model", "input": [...]}`
  in batches of 16, headers `Authorization: Bearer <key>` and `x-bf-vk: <key>`;
  documents prefixed with `DocumentPrompt`, the query with `QueryPrompt`.
- **Search:** an empty query or one starting with `/` returns "" with no
  request. Otherwise the cosine of the query against the items the scope can
  see (common, plus gateway for a gateway caller), at least `MinScore`, best
  first, at most `TopK`.
- **The embedder down** (an error, or `SearchTimeout`): a warning in the log,
  no embedding requests for `FailureCooldown`, and meanwhile (and when the
  index holds no facts) the keyword fallback: RAG:1855-1914 ported (tokens,
  stopwords, the BM25-like score, at least 1.2, top 3), over the facts the
  scope can see.
- **The block**, RAG:1917-1943 verbatim, "" when nothing matched:
  `<recalled-notes>`, the reference line, one `[<label>] <text>` line per hit
  (text whitespace-collapsed, `<\s*/?\s*recalled-notes` escaped to `&lt;`),
  `</recalled-notes>`.
- **Similar:** the scope's facts with cosine at least 0.92 to `text`; nil
  when the embedder cannot answer (the store then uses Jaccard).
- **ConfigFrom:** `[memory.rag]` `enabled`, `base_url`, `api_key` or
  `api_key_env` (the environment, then `.env`), `model` (default `bge-m3`),
  `top_k` (5), `min_score` (0.5, or 0.65 for an `embeddinggemma` model; an
  explicit value wins), `document_prompt` / `query_prompt` (for an
  embeddinggemma model, "title: none | text: " and "task: search result |
  query: " unless set), `search_timeout_seconds` (3), `failure_cooldown_seconds`
  (60), `notes_dir`, `max_chunk_chars`.

## config

`[memory]` gains `fact_char_limit` and `write_approval` (bool), and the
`[memory.rag]` table above.

## package agent

`Runner.Recall func(ctx context.Context, text string) string`, nil for lean
runs. `Send` calls it once with the turn's text; a non-empty block makes that
user row's `APIContent` the text, a blank line, and the block (`Content` stays
the text). Stored, so it replays byte for byte.

## server and cmd/soa-d

- One `memory.Store` per daemon (limits from config, `Similar` from the index
  when recall is on) in every session's `Env`, `MemoryScope` "common" (every
  soa-d session is common until phase 5), `MemoryApproval` from config.
- The session approver handles `Kind: "memory"`: `approval_kind` "memory",
  choices `once` and `deny`.
- The index opens with the daemon when `ConfigFrom` says so; interactive
  sessions get `Runner.Recall` (scope common).
- `query` (full memory): the store and recall; rule changes are denied
  (nobody to ask). `oneshot` (lean): the store, no recall.
- Rule changes never touch a running session's prompt; new sessions read
  `rules.toml` as before.

## The TUI (Python)

An `approval.request` with `approval_kind` "memory" opens the approval modal
titled "Save to memory?" with the subject and reasons as its detail and the
choices labelled "Save" (`once`) and "Don't save" (`deny`).

## README

Status: drop the memory tool and recall from "Not in the daemon yet" (leave
`session_search`, phase 3c). Memory section: recall now runs in soa-d too.

## Not in 3b

`session_search` and the wall's enforcement (3c), staging memory writes for a
later `/memory approve`, `memory.rag.sources`, transcript indexing, legacy
memory migration (the Python already did it on every instance it runs).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 3b (memory
tool, recall)`, body listing what changed, ending with the `Model:` line.

## Phase notes: the Python error messages (MT)

"Invalid kind '<k>'. Use 'rule' or 'fact'." · "Unknown action '<a>'. Use add,
replace or remove. There is no read action: rules are already in your system
prompt and relevant facts arrive with the turn." · "'text' is required for
'<action>'." · "'id' is required for '<action>'." · "Refusing to write
<name>: <error>. The file is left exactly as it is -- the user must fix it by
hand, then retry." · "Fact is <n> chars; the limit is <limit>. Split it into
several facts, or put the details in a note under notes/ and save a pointer
fact." · "A near-identical fact already exists; use action='replace' with its
id instead of adding." · "No <kind> with id '<id>'<where>." (`<where>` is "
in scope <scopes>" for rules, " in <file>" for facts) · "The rules store is
full: this change makes it <new> chars; the cap is <cap>. Nothing was changed
and nothing is removed automatically. Tell the user it is full and ask which
existing rule they want to retire (or to shorten); do not pick one yourself."
