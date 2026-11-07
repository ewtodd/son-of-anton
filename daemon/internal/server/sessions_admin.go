package server

import (
	"encoding/json"
	"strings"
)

// rename is session.rename: the user's title, which wins over any other.
func (s *Server) rename(params json.RawMessage, bad func(error) (any, *rpcError)) (any, *rpcError) {
	var p struct {
		Session string `json:"session"`
		Title   string `json:"title"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return bad(err)
	}
	if _, e := s.socketSession(p.Session); e != nil {
		return nil, e
	}
	t := strings.TrimSpace(p.Title)
	if t == "" {
		return nil, errf(codeBadParams, "title is empty")
	}
	if _, err := s.st.SetTitle(s.base, p.Session, t, "user"); err != nil {
		return nil, notFound(err, p.Session)
	}
	if l := s.loaded(p.Session); l != nil {
		l.mu.Lock()
		l.title = t
		l.emitSession()
		l.mu.Unlock()
	}
	return map[string]any{}, nil
}

// retitle is session.retitle: the session's own model names the conversation
// where it stands now, instead of the little titles model naming its opening
// message. The ask runs in the background; session.updated carries the name
// when it lands.
func (s *Server) retitle(params json.RawMessage, bad func(error) (any, *rpcError)) (any, *rpcError) {
	var p struct {
		Session string `json:"session"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return bad(err)
	}
	if _, e := s.socketSession(p.Session); e != nil {
		return nil, e
	}
	l, err := s.load(s.base, p.Session)
	if err != nil {
		return nil, notFound(err, p.Session)
	}
	if !l.retitle() {
		return nil, errf(codeInternal, "a retitle is already running")
	}
	return map[string]any{}, nil
}

// deleteSession is session.delete: the session, its children and everything
// stored for them, refused while any of them is loaded (a turn runs, a client
// is attached, or something still works in the background). The server's
// lock is held so that nothing loads one meanwhile.
func (s *Server) deleteSession(id string) (any, *rpcError) {
	if _, e := s.socketSession(id); e != nil {
		return nil, e
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ids, err := s.st.SessionTree(s.base, id)
	if err != nil {
		return nil, notFound(err, id)
	}
	for _, sid := range ids {
		if s.live[sid] != nil {
			return nil, errf(codeBusy, "session %q is in use", id)
		}
	}
	if err := s.st.DeleteSessions(s.base, ids); err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	return map[string]any{}, nil
}
