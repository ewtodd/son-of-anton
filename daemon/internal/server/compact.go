package server

import (
	"context"
	"errors"
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
// remembering each model's context length. An unreachable provider is
// remembered as having none until the next refresh.
func (s *Server) fetchMeta(ctx context.Context, ep config.Endpoint, force bool) {
	s.metaMu.Lock()
	_, have := s.meta[ep.BaseURL]
	s.metaMu.Unlock()
	if have && !force {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, metaTimeout)
	infos, _ := provider.New(ep.BaseURL, ep.APIKey).ModelInfo(ctx)
	cancel()
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
	if n := s.meta[ep.BaseURL][ep.Model]; n > 0 {
		return n
	}
	return s.learned[id]
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
	if (l.kind != "interactive" && l.kind != "worker") || l.title != "" {
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

// refreshMeta replaces what is known about a provider's models (model.list).
func (s *Server) refreshMeta(ep config.Endpoint, infos []provider.ModelInfo) {
	got := map[string]int{}
	for _, m := range infos {
		got[m.ID] = m.ContextLength
	}
	s.metaMu.Lock()
	s.meta[ep.BaseURL] = got
	s.metaMu.Unlock()
}
