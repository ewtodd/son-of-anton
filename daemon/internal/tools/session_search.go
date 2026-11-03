package tools

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

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

const (
	contentMax   = 4000
	bookendMax   = 1200
	readAll      = 30
	readHead     = 20
	readTail     = 10
	browseMax    = 20
	searchScan   = 200
	defaultLimit = 3
	maxLimit     = 10
)

var browseKinds = []string{"interactive", "chat", "oneshot"}

// sessionSearchTool is ../tools/session_search_tool.py, behind the wall.
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
			"- session_id + around_message_id: read a window around one message. To scroll, pass the window's last (forward) or first (backward) message id.\n" +
			"- session_id alone: read a whole session (the first 20 and last 10 messages if long).\n" +
			"- no arguments: list recent sessions.\n\n" +
			"Query words are ANDed; use OR, \"quoted phrases\", NOT and prefix* as needed.",
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
	sr := &searcher{env: env, caller: env.MemoryScope}
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

	sid := argStr(in, "session_id")
	anchor, hasAnchor := argInt(in, "around_message_id")
	switch {
	case sid != "" && hasAnchor:
		w, ok := argInt(in, "window")
		if !ok {
			w = 5
		}
		return sr.window(ctx, sid, anchor, int(min(max(w, 1), 20)))
	case sid != "":
		return sr.read(ctx, sid)
	case hasAnchor:
		return searchFail("around_message_id needs a session_id.")
	case argStr(in, "query") != "":
		limit, ok := argInt(in, "limit")
		if !ok {
			limit = defaultLimit
		}
		return sr.discover(ctx, argStr(in, "query"), int(min(max(limit, 1), maxLimit)), argStr(in, "sort"), argStr(in, "detail") == "full")
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
	if id == sr.env.SessionID || !store.Readable(sr.caller, s) {
		return store.Session{}, false, nil
	}
	return s, true, nil
}

func notFound(id string) (string, error) { return searchFail("session_id not found: " + id) }

func stamp(t time.Time) string { return t.Format(time.RFC3339) }

func cut(s string, max int) string {
	if r := []rune(s); len(r) > max {
		return string(r[:max])
	}
	return s
}

// msgObj is {seq, role, content, ts}; an assistant row with tool calls ends
// with "[called <name>, <name>]".
func msgObj(m store.Message, max int) map[string]any {
	content := cut(m.Content, max)
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
		out = append(out, msgObj(m, max))
	}
	return out
}

func (sr *searcher) browse(ctx context.Context) (string, error) {
	sums, err := sr.env.Store.ListReadable(ctx, sr.caller, browseKinds, browseMax+1)
	if err != nil {
		return searchFail("listing sessions failed: " + err.Error())
	}
	results := []map[string]any{}
	for _, s := range sums {
		if s.Session.ID == sr.env.SessionID || len(results) == browseMax {
			continue
		}
		results = append(results, map[string]any{"session_id": s.Session.ID, "title": s.Session.Title, "kind": s.Session.Kind,
			"started_at": stamp(s.Session.Created), "last_active": stamp(s.Updated), "message_count": s.Messages, "preview": s.Preview})
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
	total, truncated := len(shown), false
	if total > readAll {
		shown = append(shown[:readHead:readHead], shown[total-readTail:]...)
		truncated = true
	}
	return jsonResult(map[string]any{"success": true, "mode": "read", "session_id": id,
		"session_meta":  map[string]any{"title": s.Title, "kind": s.Kind, "model": s.Model, "started_at": stamp(s.Created)},
		"message_count": total, "truncated": truncated, "messages": shown})
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

func (sr *searcher) window(ctx context.Context, id string, anchor int64, w int) (string, error) {
	if _, ok, err := sr.readable(ctx, id); err != nil {
		return searchFail("reading the session failed: " + err.Error())
	} else if !ok {
		return notFound(id)
	}
	rows, ok, err := sr.around(ctx, id, anchor, w)
	if err != nil {
		return searchFail("reading the session failed: " + err.Error())
	}
	if !ok {
		return searchFail(fmt.Sprintf("around_message_id %d is not a message of session %s", anchor, id))
	}
	return jsonResult(map[string]any{"success": true, "mode": "window", "session_id": id, "anchor_seq": anchor,
		"messages": sr.objs(rows, contentMax, nil)})
}

// found is one session in a discover result.
type found struct {
	id    string
	seq   int64
	match store.Message
}

func (sr *searcher) discover(ctx context.Context, query string, limit int, sortBy string, full bool) (string, error) {
	hits, err := sr.env.Store.SearchMessages(ctx, store.MessageQuery{Query: query, Caller: sr.caller, Roles: sr.roles,
		Sort: sortBy, Exclude: sr.env.SessionID, Limit: searchScan})
	if err != nil {
		return searchFail("search failed: " + err.Error())
	}
	var order []found
	seen := map[string]bool{}
	for _, h := range hits {
		if !seen[h.SessionID] {
			seen[h.SessionID] = true
			order = append(order, found{id: h.SessionID, seq: h.Seq})
		}
	}
	if sr.env.SearchMessages != nil && len(order) < limit {
		for _, h := range sr.env.SearchMessages(ctx, query, limit*3) {
			if seen[h.SessionID] {
				continue
			}
			if _, ok, err := sr.readable(ctx, h.SessionID); err != nil || !ok {
				continue // the wall again: the index is not trusted
			}
			seen[h.SessionID] = true
			order = append(order, found{id: h.SessionID, seq: h.Seq})
		}
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
		res := map[string]any{"session_id": f.id, "title": sess.Title, "started_at": stamp(sess.Created), "last_active": stamp(last),
			"match": map[string]any{"seq": match.Seq, "role": match.Role, "content": cut(match.Content, contentMax)}}
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
