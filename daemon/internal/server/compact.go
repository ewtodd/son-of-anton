package server

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/title"
)

const metaTimeout = 3 * time.Second

// fetchMeta reads the provider's /models once per base URL (force: again),
// remembering each model's context length. A failed fetch stores nothing —
// an empty listing is not a valid status-less one — and is remembered in
// metaFail, so the catalog's negative cache works and later loads do not
// each pay the timeout.
func (s *Server) fetchMeta(ctx context.Context, ep config.Endpoint, force bool) {
	s.metaMu.Lock()
	_, have := s.meta[ep.BaseURL]
	failed := time.Since(s.metaFail[ep.BaseURL]) < catalogFailTTL
	s.metaMu.Unlock()
	if (have || failed) && !force {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, metaTimeout)
	infos, err := provider.New(ep.BaseURL, ep.APIKey).ModelInfo(ctx)
	cancel()
	if err != nil {
		s.metaMu.Lock()
		s.metaFail[ep.BaseURL] = time.Now()
		s.metaMu.Unlock()
		return
	}
	s.refreshMeta(ep, infos)
}

// contextLength is a session's window: the model's route, then the
// provider's /models entry, then a limit learned from an overflow error, else
// 0 (the agent's default).
func (s *Server) contextLength(ep config.Endpoint, id string) int {
	if n := s.cfg.RouteContextLength(ep.Model); n > 0 {
		return n
	}
	s.metaMu.Lock()
	defer s.metaMu.Unlock()
	if e := s.meta[ep.BaseURL][ep.Model]; e.context > 0 {
		return e.context
	}
	return s.learned[id]
}

// servedWindow is the window of the model that answered a session on model,
// when that is another model: under a routed id (maitred's auto, or fiona) the
// routed id's own listing is only the largest window on offer. 0 when the same
// model served, or its window is unknown.
func (s *Server) servedWindow(model, served string) int {
	if served == "" || served == model {
		return 0
	}
	if n := s.cfg.RouteContextLength(served); n > 0 {
		return n
	}
	ep, err := s.cfg.Endpoint(model)
	if err != nil {
		return 0
	}
	s.metaMu.Lock()
	defer s.metaMu.Unlock()
	return s.meta[ep.BaseURL][served].context
}

// summarizer is the summary model for a session on ep: the compaction
// auxiliary endpoint, with nil and "" standing for the session's own.
func (s *Server) summarizer(ep config.Endpoint) (*provider.Client, string) {
	aux := s.cfg.Auxiliary("compaction", ep)
	var c *provider.Client
	if aux.BaseURL != ep.BaseURL || aux.APIKey != ep.APIKey {
		c = provider.New(aux.BaseURL, aux.APIKey)
	}
	model := aux.Model
	if model == ep.Model {
		model = ""
	}
	return c, model
}

// compaction is the Runner's compaction settings for a session on ep.
func (s *Server) compaction(ep config.Endpoint, id string) agent.Compaction {
	c, model := s.summarizer(ep)
	return agent.Compaction{
		Enabled:       s.cfg.CompactionEnabled(),
		Threshold:     s.cfg.CompactionThreshold(),
		ContextLength: s.contextLength(ep, id),
		Client:        c,
		Model:         model,
	}
}

// compactCommand is command.run compact: "--preview" (or "--dry-run") reports
// the plan, anything else is the focus topic.
func (l *live) compactCommand(args string) (any, *rpcError) {
	args = strings.TrimSpace(args)
	l.mu.Lock()
	if l.running || l.compacting {
		l.mu.Unlock()
		return nil, errf(codeBusy, "a turn or compaction is running")
	}
	if args == "--preview" || args == "--dry-run" {
		l.mu.Unlock()
		plan, err := l.sess.PlanCompaction()
		if err != nil {
			return nil, compactErr(err)
		}
		return map[string]any{"preview": map[string]any{"rows": plan.Rows, "through_seq": plan.ThroughSeq, "tokens": plan.Tokens}}, nil
	}
	l.compacting = true
	l.mu.Unlock()
	res, err := l.sess.Compact(l.srv.base, args)
	l.mu.Lock()
	defer l.mu.Unlock()
	l.compacting = false
	defer l.next() // what queued while compacting
	if err != nil {
		return nil, compactErr(err)
	}
	l.compacted(res)
	return map[string]any{"through_seq": res.ThroughSeq, "summary": res.Summary}, nil
}

func compactErr(err error) *rpcError {
	if errors.Is(err, agent.ErrNothingToCompact) {
		return errf(codeBadParams, "nothing to compact: the conversation is already within the rows kept verbatim")
	}
	return errf(codeInternal, "compaction failed: %v", err)
}

// compacted announces a finished compaction (mu held).
func (l *live) compacted(res agent.CompactResult) {
	l.compactions++
	l.emit("compaction", map[string]any{"summary": res.Summary, "through_seq": res.ThroughSeq, "before_tokens": res.BeforeTokens})
	l.emitSession()
}

// nameSession titles a session that has none (mu held): at once from the
// opening message, then, in the background, from the title model.
func (l *live) nameSession(text string) {
	if l.kind != "interactive" || l.title != "" {
		return
	}
	d := title.Derive(text)
	if d == "" {
		return
	}
	if changed, err := l.srv.st.SetTitle(l.srv.base, l.id, d, "derived"); err != nil || !changed {
		return
	}
	l.title = d
	// Without a configured title model the derived title stands: asking the
	// session's own model would cost a request on every new session.
	if !l.srv.cfg.HasAuxiliary("title_generation") {
		return
	}
	ep, err := l.srv.cfg.Endpoint(l.model)
	if err != nil {
		return
	}
	aux := l.srv.cfg.Auxiliary("title_generation", ep)
	client := provider.New(aux.BaseURL, aux.APIKey)
	l.srv.turns.Add(1)
	go func() {
		defer l.srv.turns.Done()
		t, err := title.Generate(l.srv.base, client, aux.Model, text)
		if err != nil {
			return
		}
		if changed, err := l.srv.st.SetTitle(l.srv.base, l.id, t, "llm"); err != nil || !changed {
			return
		}
		l.mu.Lock()
		defer l.mu.Unlock()
		l.title = t
		l.emitSession()
	}()
}

// retitle asks the session's own model to name the conversation from a
// digest of its recent messages, wherever the conversation began. Returns
// whether the ask started (one runs at a time). mu released.
func (l *live) retitle() bool {
	l.mu.Lock()
	if l.retitling || l.closing || l.unloaded {
		l.mu.Unlock()
		return false
	}
	l.retitling = true
	id, model := l.id, l.model
	l.mu.Unlock()

	ep, err := l.srv.cfg.Endpoint(model)
	if err != nil {
		l.retitleDone(false, id, "", err)
		return true
	}
	client := provider.New(ep.BaseURL, ep.APIKey)
	l.srv.turns.Add(1)
	go func() {
		defer l.srv.turns.Done()
		defer func() { l.retitleDone(false, id, "", nil) }()
		ctx, cancel := context.WithTimeout(l.srv.base, 2*time.Minute)
		defer cancel()
		ms, _, err := l.srv.st.MessagesBefore(ctx, id, 0, 40)
		if err != nil {
			l.retitleDone(false, id, "", err)
			return
		}
		t, err := title.Retitle(ctx, client, ep.Model, digestOf(ms))
		if err != nil {
			l.retitleDone(false, id, "", err)
			return
		}
		l.retitleDone(true, id, t, nil)
	}()
	return true
}

// retitleDone finishes a background retitle: it marks the ask over, and on
// success stores the name and tells every viewer. err is the failure, if
// any, to surface to the feed.
func (l *live) retitleDone(stored bool, id, title string, err error) {
	defer l.maybeEmitNotice(err)
	l.mu.Lock()
	l.retitling = false
	if !stored || err != nil {
		l.mu.Unlock()
		return
	}
	changed, err2 := l.srv.st.SetTitle(l.srv.base, id, title, "retitle")
	if err2 != nil || !changed {
		l.mu.Unlock()
		return
	}
	l.title = title
	l.emitSession()
	l.mu.Unlock()
}

// maybeEmitNotice tells the viewers about a failed retitle; the feed shows
// notices. nil says nothing happened.
func (l *live) maybeEmitNotice(err error) {
	if err == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.emit("notice", map[string]any{"level": "warning", "text": "retitling failed: " + err.Error()})
}

// digestOf renders the recent messages the retitle ask names.
func digestOf(ms []store.Message) string {
	var b strings.Builder
	for _, m := range ms {
		if m.Role != "user" && m.Role != "assistant" {
			continue
		}
		c := strings.TrimSpace(strings.ReplaceAll(m.Content, "\n", " "))
		if c == "" {
			continue
		}
		fmt.Fprintf(&b, "%s: %s\n", m.Role, c)
	}
	return strings.TrimRight(b.String(), "\n")
}

// setTitle is session.set title: the user's title wins over any other.
func (l *live) setTitle(t string) error {
	changed, err := l.srv.st.SetTitle(l.srv.base, l.id, t, "user")
	if err != nil || !changed {
		return err
	}
	l.mu.Lock()
	l.title = t
	l.mu.Unlock()
	return nil
}

// compactionObjs is the attach result's list of compactions.
func compactionObjs(cs []store.Compaction) []map[string]any {
	out := make([]map[string]any, len(cs))
	for i, c := range cs {
		out[i] = map[string]any{"through_seq": c.ThroughSeq, "summary": c.Summary, "created": c.Created.Format(time.RFC3339)}
	}
	return out
}

// refreshMeta replaces what is known about a provider's models (model.list):
// each model's context length and its live status ("serving"/"unavailable"/>
// "" from a server that does not say). It stores only successful listings
// and clears the endpoint's remembered failure, since the endpoint just
// answered.
func (s *Server) refreshMeta(ep config.Endpoint, infos []provider.ModelInfo) {
	got := map[string]metaEntry{}
	for _, m := range infos {
		got[m.ID] = metaEntry{context: m.ContextLength, status: lowerStatus(m.Status)}
	}
	s.metaMu.Lock()
	s.meta[ep.BaseURL] = got
	s.metaAt[ep.BaseURL] = time.Now()
	delete(s.metaFail, ep.BaseURL)
	s.metaMu.Unlock()
}

// lowerStatus normalizes a status so only "unavailable" marks a model down.
func lowerStatus(st string) string {
	st = strings.ToLower(strings.TrimSpace(st))
	if st == "unavailable" {
		return "unavailable"
	}
	return st // "serving", or whatever else an endpoint says
}
