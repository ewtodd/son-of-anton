package tools

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// MessageHit is a meaning-based match on one conversation message.
type MessageHit struct {
	SessionID string
	Seq       int64
	Text      string
	Score     float64
}

// MessageSearch finds messages by meaning (nil in Env when recall is off).
// Its results are untrusted: the tool applies the wall to them again.
type MessageSearch func(ctx context.Context, query string, k int) []MessageHit

// Reranker gives each document's relevance to query, in the documents'
// order (nil in Env when no reranker is configured). session_search uses it
// to order its matches when the caller asks for relevance (no sort), and
// keeps its own order when it fails.
type Reranker func(ctx context.Context, query string, docs []string) ([]float64, error)

const (
	// rerankFrame introduces the query: the reranker is trained to find
	// passages that answer one, and a match is often the question itself.
	// Framed, a past question about the query scores like an answer to it.
	rerankFrame = "Find the past conversation about: "
	// rerankDocMax is how much of a matched message the reranker reads, and
	// rerankMax how many matches one search sends it.
	rerankDocMax = 1200
	rerankMax    = 24

	contentMax   = 4000
	bookendMax   = 1200
	readAll      = 30
	readHead     = 20
	readTail     = 10
	browseMax    = 20
	defaultLimit = 3
	maxLimit     = 10
)

var browseKinds = []string{"interactive", "chat", "oneshot"}

// sessionSearchTool searches past sessions, behind the wall.
type sessionSearchTool struct{}

func (sessionSearchTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "session_search",
		Description: "Search or read past son-of-anton conversations (full-text search over the session store, plus meaning-based " +
			"matching when recall is enabled; returns the real messages). It only knows what was said here before: when the " +
			"user names a live source (a URL, file, contact, account or system), check that source first, and don't conclude " +
			"something doesn't exist from this search alone.\n\n" +
			"Shapes:\n" +
			"- query: find sessions. The best match comes with its opening and closing messages and the messages around the hit; the others with just the matching message.\n" +
			"- session_id + query: find messages inside that session and read around the best one.\n" +
			"- session_id + around_message_id: read a window around one message. To scroll, pass the window's last (forward) or first (backward) message id. A message that ends in a [cut: ...] note continues with content_offset.\n" +
			"- session_id alone: read a whole session (the first 20 and last 10 messages if long). window and detail do not apply here.\n" +
			"- no arguments: list recent sessions, newest first. Use this, not a query, to find the most recent one.\n\n" +
			"Query words are ANDed; use OR, \"quoted phrases\", NOT and prefix* as needed.\n\n" +
			"Results give a session's cwd (the directory it ran in) and its state: running, idle, queued, or picked up (a terminal is driving it). " +
			"A running session is still working, however long ago its last message was.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "Words or an FTS5 expression to search for. Omit to list recent sessions."},
    "limit": {"type": "integer", "description": "Sessions to return when searching (default 3, max 10)."},
    "sort": {"type": "string", "enum": ["newest", "oldest"], "description": "'newest' for where-did-we-leave-off questions, 'oldest' for how-did-it-start ones; omit to rank by relevance."},
    "detail": {"type": "string", "enum": ["adaptive", "full"], "description": "'full' gives every result its context; the default 'adaptive' only the top one."},
    "session_id": {"type": "string", "description": "Session to read, from a search result."},
    "around_message_id": {"type": "integer", "description": "Message id to center the window on, e.g. a result's match seq."},
    "window": {"type": "integer", "description": "Messages on each side of the anchor (1-20, default 5)."},
    "content_offset": {"type": "integer", "description": "With around_message_id: read only that message, from this character on. A [cut: ...] note gives the number."},
    "role_filter": {"type": "string", "description": "Comma-separated roles to search; default 'user,assistant'. Add 'tool' to include tool output."}
  }
}`),
	}}
}

func searchFail(msg string) (string, error) {
	return jsonResult(map[string]any{"success": false, "error": msg})
}

// argInt reads a lenient integer argument (a number or a numeric string).
func argInt(in map[string]any, key string) (int64, bool) {
	switch v := in[key].(type) {
	case float64:
		return int64(v), true
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
		return n, err == nil
	}
	return 0, false
}

func argStr(in map[string]any, key string) string {
	s, _ := in[key].(string)
	return strings.TrimSpace(s)
}

type searcher struct {
	env     *Env
	caller  string
	chat    string // the chat a gateway caller is walled to ("": none)
	roles   []string
	roleSet map[string]bool
}

func (sessionSearchTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	if env.Store == nil {
		return searchFail("session_search is not available: there is no session store in this run.")
	}
	in := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &in); err != nil {
			return errorResult("invalid arguments: " + err.Error()), nil
		}
	}
	sr := &searcher{env: env, caller: env.MemoryScope, chat: env.WallChat}
	sr.roles = []string{"user", "assistant"}
	if rf := argStr(in, "role_filter"); rf != "" {
		var got []string
		for _, r := range strings.Split(rf, ",") {
			switch r = strings.ToLower(strings.TrimSpace(r)); r {
			case "user", "assistant", "tool":
				got = append(got, r)
			}
		}
		if len(got) > 0 {
			sr.roles = got
		}
	}
	sr.roleSet = map[string]bool{}
	for _, r := range sr.roles {
		sr.roleSet[r] = true
	}

	sid, query := argStr(in, "session_id"), argStr(in, "query")
	anchor, hasAnchor := argInt(in, "around_message_id")
	offset, _ := argInt(in, "content_offset")
	w, hasWindow := argInt(in, "window")
	if !hasWindow {
		w = 5
	}
	window := int(min(max(w, 1), 20))
	limit, ok := argInt(in, "limit")
	if !ok {
		limit = defaultLimit
	}
	limit = min(max(limit, 1), maxLimit)
	switch {
	case sid != "" && hasAnchor:
		return sr.window(ctx, sid, anchor, window, int(max(offset, 0)))
	case offset > 0:
		return searchFail("content_offset needs a session_id and around_message_id.")
	case sid != "" && query != "":
		return sr.inSession(ctx, sid, query, window, int(limit), argStr(in, "sort"))
	case sid != "":
		if hasWindow || argStr(in, "detail") == "full" {
			return searchFail("session_id alone reads the first 20 and last 10 messages of a long session; window and detail do not change that. " +
				"To read the rest, pass around_message_id (any seq), or query to find a message inside the session.")
		}
		return sr.read(ctx, sid)
	case hasAnchor:
		return searchFail("around_message_id needs a session_id.")
	case query != "":
		return sr.discover(ctx, query, int(limit), argStr(in, "sort"), argStr(in, "detail") == "full")
	}
	return sr.browse(ctx)
}

// readable loads a session the caller may read and that is not its own. A
// walled, own or missing session is the same answer.
func (sr *searcher) readable(ctx context.Context, id string) (store.Session, bool, error) {
	s, err := sr.env.Store.Session(ctx, id)
	if errors.Is(err, store.ErrNotFound) {
		return store.Session{}, false, nil
	}
	if err != nil {
		return store.Session{}, false, err
	}
	if id == sr.env.SessionID || !store.ReadableFrom(sr.caller, sr.chat, s) {
		return store.Session{}, false, nil
	}
	return s, true, nil
}

func notFound(id string) (string, error) { return searchFail("session_id not found: " + id) }

func stamp(t time.Time) string { return t.Format(time.RFC3339) }

// cut is at most max characters of a message's text, from character from. A
// cut says so and how to go on: a text that just stops reads as one its author
// never finished.
func cut(m store.Message, from, max int) string {
	r := []rune(m.Content)
	total := len(r)
	r = r[min(from, total):]
	if len(r) <= max {
		return string(r)
	}
	return fmt.Sprintf("%s\n[cut: showing %d of %d characters; for the rest pass around_message_id %d, content_offset %d]",
		string(r[:max]), max, total, m.Seq, from+max)
}

// msgObj is {seq, role, content, ts}; an assistant row with tool calls ends
// with "[called <name>, <name>]".
func msgObj(m store.Message, from, max int) map[string]any {
	content := cut(m, from, max)
	if m.Role == "assistant" && len(m.ToolCalls) > 0 {
		var calls []struct {
			Function struct {
				Name string `json:"name"`
			} `json:"function"`
		}
		if json.Unmarshal(m.ToolCalls, &calls) == nil && len(calls) > 0 {
			names := make([]string, len(calls))
			for i, c := range calls {
				names[i] = c.Function.Name
			}
			if content != "" {
				content += "\n"
			}
			content += "[called " + strings.Join(names, ", ") + "]"
		}
	}
	return map[string]any{"seq": m.Seq, "role": m.Role, "content": content, "ts": stamp(m.TS)}
}

func (sr *searcher) objs(ms []store.Message, max int, skip map[int64]bool) []map[string]any {
	out := []map[string]any{}
	for _, m := range ms {
		if !sr.roleSet[m.Role] || skip[m.Seq] {
			continue
		}
		out = append(out, msgObj(m, 0, max))
	}
	return out
}

// addState adds what the daemon says the session is doing now.
func (sr *searcher) addState(ctx context.Context, m map[string]any, id string) {
	if sr.env.SessionState != nil {
		m["state"] = sr.env.SessionState(ctx, id)
	}
}

// meta is what a reader needs to act on a session: where it ran and whether
// it still runs.
func (sr *searcher) meta(ctx context.Context, s store.Session) map[string]any {
	m := map[string]any{"title": s.Title, "kind": s.Kind, "model": s.Model, "cwd": s.Cwd, "started_at": stamp(s.Created)}
	sr.addState(ctx, m, s.ID)
	return m
}

func (sr *searcher) browse(ctx context.Context) (string, error) {
	sums, err := sr.env.Store.ListReadableIn(ctx, sr.caller, sr.chat, "", browseKinds, browseMax+1)
	if err != nil {
		return searchFail("listing sessions failed: " + err.Error())
	}
	results := []map[string]any{}
	for _, s := range sums {
		if s.Session.ID == sr.env.SessionID || len(results) == browseMax {
			continue
		}
		entry := map[string]any{"session_id": s.Session.ID, "title": s.Session.Title, "kind": s.Session.Kind, "cwd": s.Session.Cwd,
			"started_at": stamp(s.Session.Created), "last_active": stamp(s.Updated), "message_count": s.Messages, "preview": s.Preview}
		sr.addState(ctx, entry, s.Session.ID)
		results = append(results, entry)
	}
	return jsonResult(map[string]any{"success": true, "mode": "browse", "results": results, "count": len(results)})
}

func (sr *searcher) read(ctx context.Context, id string) (string, error) {
	s, ok, err := sr.readable(ctx, id)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	if !ok {
		return notFound(id)
	}
	all, err := sr.env.Store.Messages(ctx, id)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	shown := sr.objs(all, contentMax, nil)
	total := len(shown)
	res := map[string]any{"success": true, "mode": "read", "session_id": id, "session_meta": sr.meta(ctx, s),
		"message_count": total, "truncated": total > readAll}
	if total > readAll {
		res["omitted"] = fmt.Sprintf("%d messages between seq %v and seq %v are not shown: read them with around_message_id, or find one with query.",
			total-readAll, shown[readHead-1]["seq"], shown[total-readTail]["seq"])
		shown = append(shown[:readHead:readHead], shown[total-readTail:]...)
	}
	res["messages"] = shown
	return jsonResult(res)
}

// around returns the rows from anchor-w to anchor+w and whether the anchor
// is a row of the session.
func (sr *searcher) around(ctx context.Context, id string, anchor int64, w int) ([]store.Message, bool, error) {
	before, _, err := sr.env.Store.MessagesBefore(ctx, id, anchor, w)
	if err != nil {
		return nil, false, err
	}
	after, _, err := sr.env.Store.MessagesAfter(ctx, id, anchor-1, w+1)
	if err != nil {
		return nil, false, err
	}
	if len(after) == 0 || after[0].Seq != anchor {
		return nil, false, nil
	}
	return append(before, after...), true, nil
}

// window reads around one message; a content offset reads on in that message
// alone.
func (sr *searcher) window(ctx context.Context, id string, anchor int64, w, offset int) (string, error) {
	s, ok, err := sr.readable(ctx, id)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	if !ok {
		return notFound(id)
	}
	rows, ok, err := sr.around(ctx, id, anchor, w)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	if !ok {
		return searchFail(fmt.Sprintf("around_message_id %d is not a message of session %s", anchor, id))
	}
	msgs := sr.objs(rows, contentMax, nil)
	if offset > 0 {
		msgs = []map[string]any{}
		for _, m := range rows {
			if m.Seq != anchor || !sr.roleSet[m.Role] {
				continue
			}
			if n := len([]rune(m.Content)); offset >= n {
				return searchFail(fmt.Sprintf("content_offset %d is past the end of message %d, which has %d characters", offset, anchor, n))
			}
			msgs = append(msgs, msgObj(m, offset, contentMax))
		}
	}
	return jsonResult(map[string]any{"success": true, "mode": "window", "session_id": id, "anchor_seq": anchor,
		"session_meta": sr.meta(ctx, s), "messages": msgs})
}

// inSession finds query inside one session and reads around the best match.
func (sr *searcher) inSession(ctx context.Context, id, query string, w, limit int, sortBy string) (string, error) {
	s, ok, err := sr.readable(ctx, id)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	if !ok {
		return notFound(id)
	}
	rerank := sr.env.Rerank != nil && sortBy == ""
	fetch := limit
	if rerank {
		fetch = 2 * limit
	}
	hits, err := sr.env.Store.SearchMessages(ctx, store.MessageQuery{Query: query, Caller: sr.caller, Chat: sr.chat, Roles: sr.roles,
		Sort: sortBy, Session: id, Limit: fetch})
	if err != nil {
		return searchFail("search failed: " + err.Error())
	}
	if rerank {
		bySeq := map[int64]store.Hit{}
		fs := make([]found, len(hits))
		for i, h := range hits {
			bySeq[h.Seq], fs[i] = h, found{id: id, seq: h.Seq, snippet: h.Snippet, literal: true}
		}
		hits = hits[:0:0]
		for _, f := range sr.reranked(ctx, query, fs) {
			hits = append(hits, bySeq[f.seq])
		}
	}
	hits = hits[:min(len(hits), limit)]
	matches := []map[string]any{}
	for _, h := range hits {
		matches = append(matches, map[string]any{"seq": h.Seq, "role": h.Role, "snippet": h.Snippet})
	}
	res := map[string]any{"success": true, "mode": "session_query", "query": query, "session_id": id, "session_meta": sr.meta(ctx, s),
		"count": len(hits), "matches": matches, "messages": []map[string]any{}}
	if len(hits) > 0 {
		rows, ok, err := sr.around(ctx, id, hits[0].Seq, w)
		if err != nil || !ok {
			return searchFail(fmt.Sprintf("reading around message %d failed: %v", hits[0].Seq, err))
		}
		res["anchor_seq"], res["messages"] = hits[0].Seq, sr.objs(rows, contentMax, nil)
	}
	return jsonResult(res)
}

// found is one matched message: a text match's (literal, with its snippet)
// or a meaning match's (with the indexed chunk that matched).
type found struct {
	id            string
	seq           int64
	snippet, text string
	literal       bool
}

func (sr *searcher) discover(ctx context.Context, query string, limit int, sortBy string, full bool) (string, error) {
	rerank := sr.env.Rerank != nil && sortBy == ""
	fetch := limit
	if rerank {
		fetch = 2 * limit
	}
	hits, err := sr.env.Store.SearchMessages(ctx, store.MessageQuery{Query: query, Caller: sr.caller, Chat: sr.chat, Roles: sr.roles,
		Sort: sortBy, Exclude: sr.env.SessionID, Limit: fetch, PerSession: true})
	if err != nil {
		return searchFail("search failed: " + err.Error())
	}
	var order []found
	seen := map[string]bool{}
	for _, h := range hits {
		if !seen[h.SessionID] {
			seen[h.SessionID] = true
			order = append(order, found{id: h.SessionID, seq: h.Seq, snippet: h.Snippet, literal: true})
		}
	}
	if sr.env.SearchMessages != nil && (len(order) < limit || rerank) {
		for _, h := range sr.env.SearchMessages(ctx, query, limit*3) {
			if seen[h.SessionID] {
				continue
			}
			if _, ok, err := sr.readable(ctx, h.SessionID); err != nil || !ok {
				continue // the wall again: the index is not trusted
			}
			seen[h.SessionID] = true
			order = append(order, found{id: h.SessionID, seq: h.Seq, text: h.Text})
		}
	}
	if rerank {
		order = sr.reranked(ctx, query, order)
	}
	results := []map[string]any{}
	for _, f := range order {
		if len(results) == limit {
			break
		}
		rows, ok, err := sr.around(ctx, f.id, f.seq, 5)
		if err != nil || !ok {
			continue
		}
		var match store.Message
		for _, m := range rows {
			if m.Seq == f.seq {
				match = m
			}
		}
		if !sr.roleSet[match.Role] {
			continue
		}
		sess, err := sr.env.Store.Session(ctx, f.id)
		if err != nil {
			continue
		}
		last := sess.Created
		if sum, err := sr.env.Store.SessionSummary(ctx, f.id); err == nil {
			last = sum.Updated
		}
		res := map[string]any{"session_id": f.id, "title": sess.Title, "kind": sess.Kind, "cwd": sess.Cwd, "started_at": stamp(sess.Created), "last_active": stamp(last),
			"match": map[string]any{"seq": match.Seq, "role": match.Role, "content": cut(match, 0, contentMax)}}
		sr.addState(ctx, res, f.id)
		if full || len(results) == 0 {
			head, _, _ := sr.env.Store.MessagesAfter(ctx, f.id, 0, 3)
			tail, _, _ := sr.env.Store.MessagesBefore(ctx, f.id, 0, 3)
			res["context"] = map[string]any{
				"opening": sr.objs(head, bookendMax, skipSeqs(head, func(m store.Message) bool { return inRows(rows, m.Seq) })),
				"around":  sr.objs(rows, contentMax, nil),
				"closing": sr.objs(tail, bookendMax, skipSeqs(tail, func(m store.Message) bool { return inRows(rows, m.Seq) || inRows(head, m.Seq) })),
			}
		}
		results = append(results, res)
	}
	return jsonResult(map[string]any{"success": true, "mode": "discover", "query": query, "results": results, "count": len(results)})
}

func inRows(rows []store.Message, seq int64) bool {
	for _, m := range rows {
		if m.Seq == seq {
			return true
		}
	}
	return false
}

// skipSeqs returns the seqs of ms that drop says to leave out.
func skipSeqs(ms []store.Message, drop func(store.Message) bool) map[int64]bool {
	out := map[int64]bool{}
	for _, m := range ms {
		if drop(m) {
			out[m.Seq] = true
		}
	}
	return out
}

// reranked is order as the reranker ranks it: at most rerankMax matches,
// each scored on what rerankDoc reads of it, best first. Nothing is dropped:
// the reranker can score a real match as low as an unrelated one (a past
// "won't suspend since the kernel update" at 0.002 for "sleep broken after
// upgrade"), while its order put the real ones first. On any failure it is
// order as it was.
func (sr *searcher) reranked(ctx context.Context, query string, order []found) []found {
	order = order[:min(len(order), rerankMax)]
	q := rerankQuery(query)
	if len(order) == 0 || q == "" {
		return order
	}
	docs := make([]string, len(order))
	for i, f := range order {
		docs[i] = sr.rerankDoc(ctx, f)
	}
	scores, err := sr.env.Rerank(ctx, rerankFrame+q, docs)
	if err != nil || len(scores) != len(order) {
		return order
	}
	idx := make([]int, len(order))
	for i := range idx {
		idx[i] = i
	}
	sort.SliceStable(idx, func(i, j int) bool { return scores[idx[i]] > scores[idx[j]] })
	out := make([]found, len(order))
	for i, k := range idx {
		out[i] = order[k]
	}
	return out
}

// rerankDoc is what the reranker reads of a match: the session's title, then
// the message's role and a meaning match's chunk, or rerankDocMax characters
// of a text match's message centred on the first term its snippet
// highlights. A message gone since it matched leaves the chunk or snippet.
func (sr *searcher) rerankDoc(ctx context.Context, f found) string {
	text := strings.NewReplacer("«", "", "»", "", "…", "").Replace(f.snippet)
	if !f.literal {
		text = centred(f.text, "", rerankDocMax)
	}
	role := ""
	if ms, _, err := sr.env.Store.MessagesAfter(ctx, f.id, f.seq-1, 1); err == nil && len(ms) > 0 && ms[0].Seq == f.seq {
		role = ms[0].Role + ": "
		if f.literal {
			term := ""
			if _, rest, ok := strings.Cut(f.snippet, "«"); ok {
				term, _, _ = strings.Cut(rest, "»")
			}
			text = centred(ms[0].Content, term, rerankDocMax)
		}
	}
	head := ""
	if s, err := sr.env.Store.Session(ctx, f.id); err == nil && strings.TrimSpace(s.Title) != "" {
		head = fmt.Sprintf("Conversation %q\n", strings.TrimSpace(s.Title))
	}
	return head + role + text
}

// centred is n characters of text around the first occurrence of term (any
// case), or its first n when term is empty or absent.
func centred(text, term string, n int) string {
	r := []rune(text)
	if len(r) <= n {
		return text
	}
	at := 0
	if term != "" {
		low := strings.ToLower(text) // rune for rune, so rune offsets carry over
		if i := strings.Index(low, strings.ToLower(term)); i >= 0 {
			at = utf8.RuneCountInString(low[:i])
		}
	}
	start := max(0, min(at-n/2, len(r)-n))
	return string(r[start : start+n])
}

// rerankQuery is an FTS5 query as plain words for the reranker: quotes,
// prefix stars and parentheses dropped, AND and OR dropped, and NOT dropped
// with the word after it.
func rerankQuery(q string) string {
	words := strings.Fields(strings.NewReplacer(`"`, " ", "*", " ", "(", " ", ")", " ").Replace(q))
	var out []string
	for i := 0; i < len(words); i++ {
		switch words[i] {
		case "AND", "OR":
			continue
		case "NOT":
			i++
			continue
		}
		out = append(out, words[i])
	}
	return strings.Join(out, " ")
}
