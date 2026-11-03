package server

import (
	"context"
	"log"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// searchMessages is the meaning-based half of session_search for a session
// of the given scope; nil when recall is off. An embedder that does not
// answer yields no hits (the tool then has its word matches).
func (s *Server) searchMessages(scope string) tools.MessageSearch {
	if s.idx == nil {
		return nil
	}
	return func(ctx context.Context, query string, k int) []tools.MessageHit {
		hits, err := s.idx.SearchMessages(ctx, scope, query, k)
		if err != nil {
			return nil
		}
		out := make([]tools.MessageHit, len(hits))
		for i, h := range hits {
			out[i] = tools.MessageHit{SessionID: h.SessionID, Seq: h.Seq, Text: h.Text, Score: h.Score}
		}
		return out
	}
}

// indexAfterTurn indexes the session's new rows in the background. It is
// called from a turn goroutine, so the turns group is still non-empty.
func (s *Server) indexAfterTurn(id string) {
	if s.idx == nil {
		return
	}
	s.turns.Add(1)
	go func() {
		defer s.turns.Done()
		if err := s.indexSession(s.base, id); err != nil && s.base.Err() == nil {
			log.Printf("recall: indexing session %s: %v", id, err)
		}
	}()
}

// backfill indexes every session's rows, newest sessions first, and stops at
// the first failure (the embedder is down; the next turn or start retries).
func (s *Server) backfill() {
	if s.idx == nil {
		return
	}
	s.turns.Add(1)
	go func() {
		defer s.turns.Done()
		ids, err := s.st.SessionIDs(s.base)
		if err != nil {
			log.Printf("recall: listing sessions to index: %v", err)
			return
		}
		for _, id := range ids {
			if err := s.indexSession(s.base, id); err != nil {
				if s.base.Err() == nil {
					log.Printf("recall: indexing stopped at session %s: %v", id, err)
				}
				return
			}
		}
	}()
}

// indexSession passes the session's rows not yet indexed to the index. A
// session stamped with a chat counts as gateway whatever its scope says, so
// its text can never reach a common caller.
func (s *Server) indexSession(ctx context.Context, id string) error {
	sess, err := s.st.Session(ctx, id)
	if err != nil {
		return err
	}
	scope := sess.Scope
	if sess.Chat != "" {
		scope = "gateway"
	}
	s.indexedMu.Lock()
	from := s.indexed[id]
	s.indexedMu.Unlock()
	var rows []store.Message
	for {
		got, more, err := s.st.MessagesAfter(ctx, id, from, 500)
		if err != nil {
			return err
		}
		rows = append(rows, got...)
		if !more || len(got) == 0 {
			break
		}
		from = got[len(got)-1].Seq
	}
	if len(rows) == 0 {
		return nil
	}
	if err := s.idx.IndexMessages(ctx, id, scope, rows); err != nil {
		return err
	}
	s.indexedMu.Lock()
	s.indexed[id] = max(s.indexed[id], rows[len(rows)-1].Seq)
	s.indexedMu.Unlock()
	return nil
}
