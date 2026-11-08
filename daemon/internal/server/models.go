package server

// Models in sessions and chats (phase 8g): the names a session may be set to,
// a dead pin falling back, declared renames, and /model in a chat.

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// metaEntry is what the provider's /models says about a model: its context
// length and its live status ("serving", "unavailable", "" if unsaid).
type metaEntry struct {
	context int
	status  string
}

// statusTTL is how long a reported status is trusted: maitred's statuses
// describe the moment, not the catalog cache's hour, so the picker asks again
// once they are this old. A mode switch therefore shows on the next open.
const statusTTL = 10 * time.Second

// catalogTimeout bounds the picker's status lookup: fail open, and never delay
// opening the picker or attaching for more than a short timeout.
const catalogTimeout = 2 * time.Second

// catalogFailTTL is how long a failed catalog fetch is remembered, so the
// callers (attach, the chat's outage warning, model.list, the models note)
// do not each pay the catalog timeout again during an outage.
const catalogFailTTL = 5 * time.Second

// modelRoutes is every model name a session may be set to, with its
// reasoning efforts (nil for none known); the configured routes plus what the
// provider's /models lists. It carries no filtering: naming an unloaded model
// stays valid.
func (s *Server) modelRoutes() map[string][]string {
	routes := s.cfg.Routes()
	infos, ok := s.catalog()
	if ok {
		addCatalogRoutes(routes, infos)
	}
	return routes
}

// addCatalogRoutes adds the provider's /models ids to the configured routes.
func addCatalogRoutes(routes map[string][]string, infos []provider.ModelInfo) {
	for _, info := range infos {
		if _, ok := routes[info.ID]; !ok {
			routes[info.ID] = nil
		}
	}
}

// catalog is the provider's /models listing for the default endpoint, with
// its reported statuses (normalized, as lowerStatus gives them), refreshed
// when they are older than statusTTL. It fails open: a failed or status-less
// request gives no filtering at all, and a failed fetch is remembered for
// catalogFailTTL so the callers do not each pay the timeout again.
func (s *Server) catalog() ([]provider.ModelInfo, bool) {
	ep, err := s.cfg.Endpoint("")
	if err != nil {
		return nil, false
	}
	s.metaMu.Lock()
	entries, have := s.meta[ep.BaseURL]
	reports := false
	for _, e := range entries {
		if e.status != "" {
			reports = true
			break
		}
	}
	stale := reports && time.Since(s.metaAt[ep.BaseURL]) > statusTTL
	failed := time.Since(s.metaFail[ep.BaseURL]) < catalogFailTTL
	s.metaMu.Unlock()
	if have && !stale {
		out := make([]provider.ModelInfo, 0, len(entries))
		for id, e := range entries {
			out = append(out, provider.ModelInfo{ID: id, ContextLength: e.context, Status: e.status})
		}
		return out, true
	}
	if failed {
		return nil, false
	}
	return s.fetchCatalog(ep)
}

func (s *Server) fetchCatalog(ep config.Endpoint) ([]provider.ModelInfo, bool) {
	ctx, cancel := context.WithTimeout(s.base, catalogTimeout)
	defer cancel()
	infos, err := provider.New(ep.BaseURL, ep.APIKey).ModelInfo(ctx)
	if err != nil {
		s.metaMu.Lock()
		s.metaFail[ep.BaseURL] = time.Now()
		s.metaMu.Unlock()
		return nil, false
	}
	s.metaMu.Lock()
	delete(s.metaFail, ep.BaseURL)
	s.metaMu.Unlock()
	for i := range infos {
		infos[i].Status = lowerStatus(infos[i].Status)
	}
	s.refreshMeta(ep, infos)
	return infos, true
}

func sortedKeys(m map[string][]string) []string {
	names := make([]string, 0, len(m))
	for n := range m {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// defaultRoute is the route a session starts on, as the agent's fallback for
// a dead pin: the endpoint of model ("" = model.default).
func (s *Server) defaultRoute(model string) *agent.Fallback {
	ep, err := s.cfg.Endpoint(model)
	if err != nil {
		return nil
	}
	return &agent.Fallback{Client: provider.New(ep.BaseURL, ep.APIKey), Model: ep.Model, Effort: ep.ReasoningEffort}
}

// downText is what a session is told when its model is not being served:
// pick another model, or (in a chat) it may come back.
func downText(model string) string {
	return model + " is not being served; pick another model"
}

// fallbackDownText is what a chat is told when its model is not being served
// and the chat falls back to this instance's model on its own: nothing to
// pick, the next turn is answered anyway.
func fallbackDownText(model string) string {
	return model + " is not being served; asking son-of-anton"
}

// chatFallsBack reports whether a chat on model is answered by this instance's
// model when its own is down: the fallback chatRunner gives a chat whose model
// is the chat model and differs from model.default (a /model pin to another
// model has none).
func (s *Server) chatFallsBack(model string) bool {
	chat := s.cfg.ChatModel()
	return chat != "" && chat != s.cfg.Model.Default && model == chat
}

// modelStatus reports the live status of model from the refreshed catalog and
// whether it is known: "unavailable" when known down, "serving" when reported
// serving, "" when the endpoint lists it without a status. known is false when
// the lookup failed or the model is not listed, so callers can tell a failed
// lookup from a status-less model.
func (s *Server) modelStatus(model string) (string, bool) {
	ep, err := s.cfg.Endpoint(model)
	if err != nil {
		return "", false
	}
	cat, ok := s.catalog()
	if !ok {
		return "", false
	}
	for _, info := range cat {
		if info.ID == ep.Model {
			return info.Status, true
		}
	}
	return "", false
}

// deadPinText is what a session is told when its model was gone.
func deadPinText(old, def string) string {
	return old + " is no longer served; this session is back on " + def + "."
}

// deadPin stores a session's return to its default route (the agent session
// is already on it) and tells its viewers.
func (s *Server) deadPin(l *live, old string, def agent.Fallback) {
	l.mu.Lock()
	mode, kind := l.mode, l.kind
	l.mu.Unlock()
	stored, storedEffort := def.Model, def.Effort
	if kind == "chat" {
		stored, storedEffort = "", "" // back to following the instance's chat model
	}
	if err := s.st.UpdateSessionSettings(s.base, l.id, stored, storedEffort, mode); err != nil {
		log.Printf("session %s: storing the fallback from %s: %v", l.id, old, err)
	}
	ep, epErr := s.cfg.Endpoint(def.Model)
	if epErr == nil {
		s.fetchMeta(s.base, ep, false)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.model, l.effort = def.Model, def.Effort
	if epErr == nil {
		l.sess.SetContextLength(s.contextLength(ep, l.id))
		l.sess.SetSummarizer(s.summarizer(ep))
		l.ctxKnown = l.sess.ContextLength()
	}
	l.emitSession()
	l.emit("notice", map[string]any{"level": "warning", "text": deadPinText(old, def.Model)})
}

// applyRenames rewrites the sessions pinned to a renamed model to its new
// name ([models] renamed).
func (s *Server) applyRenames() {
	for old, to := range s.cfg.Models.Renamed {
		if old == "" || to == "" || old == to {
			continue
		}
		n, err := s.st.RenameModel(s.base, old, to)
		if err != nil {
			log.Printf("models: renaming %s to %s: %v", old, to, err)
		} else if n > 0 {
			log.Printf("models: %d sessions moved from %s to %s", n, old, to)
		}
	}
}

// modelCommand is a chat's /model: with no name, what the chat runs on; with
// one, switch the chat's session to it from the next message.
func (h *chatHub) modelCommand(cs *chatState, name string) string {
	s := h.srv
	if name == "" {
		model, served := s.chatModel(), ""
		if id, err := s.st.ChatSession(s.base, cs.stamp); err == nil {
			if rec, err := s.st.Session(s.base, id); err == nil && rec.Model != "" {
				model = rec.Model
			}
			if l := s.loaded(id); l != nil {
				l.mu.Lock()
				model = l.model
				l.mu.Unlock()
			}
			served = s.lastServed(id)
		}
		text := "Model: " + model
		if served != "" {
			text += "; the last reply came from " + served
		}
		return text + ". /model <name> switches this chat (e.g. /model auto); /model default follows this instance's model again."
	}
	follow := name == "default"
	if follow {
		name = s.chatModel()
	}
	names := sortedKeys(s.modelRoutes())
	known := false
	for _, n := range names {
		known = known || n == name
	}
	if !known {
		return fmt.Sprintf("Unknown model %s. Known: %s.", name, strings.Join(names, ", "))
	}
	cs.sessMu.Lock()
	defer cs.sessMu.Unlock()
	l, err := s.chatLive(s.base, cs)
	if err != nil {
		return "Sorry, that failed: " + err.Error()
	}
	params, _ := json.Marshal(map[string]any{"model": name})
	if _, e := s.set(l, params, func(err error) (any, *rpcError) { return nil, errf(codeBadParams, "%v", err) }); e != nil {
		return "Sorry, that failed: " + e.Message
	}
	if follow {
		// Stored empty, so the chat keeps following the instance's model.
		l.mu.Lock()
		mode := l.mode
		l.mu.Unlock()
		if err := s.st.UpdateSessionSettings(s.base, l.id, "", "", mode); err != nil {
			return "Sorry, that failed: " + err.Error()
		}
		return "Model: " + name + " (this instance's model), from the next message."
	}
	return "Model: " + name + ", from the next message."
}

// chatModel is the model chats follow: gateway.model, else model.default.
func (s *Server) chatModel() string {
	if m := s.cfg.ChatModel(); m != "" {
		return m
	}
	return s.cfg.Model.Default
}

// lastServed is the model that answered the session's latest reply in this
// run, "" before any.
func (s *Server) lastServed(id string) string {
	v, _ := s.served.Load(id)
	m, _ := v.(string)
	return m
}

// ---- the models note (phase 8i) ----

// windowText is a context window as the models note writes it: 128k (thousands
// rounded down) or 1M; "" when unknown.
func windowText(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%dM", n/1_000_000)
	case n >= 1000:
		return fmt.Sprintf("%dk", n/1000)
	}
	return ""
}

// workerModels are the models a chat may hand a worker: what the provider
// lists as live now (maitred's status "serving"; an entry without a status
// counts as live), less relay.hidden_models, sorted. nil when the list cannot
// be fetched. It reads /models directly — not through the catalog — because
// the catalog stops refreshing an endpoint that reports no statuses, and the
// models note must keep following the listing. A failed fetch is remembered
// in metaFail, so a chat turn during an outage does not each pay the timeout.
func (s *Server) workerModels() []string {
	ep, err := s.cfg.Endpoint("")
	if err != nil {
		return nil
	}
	s.metaMu.Lock()
	failed := time.Since(s.metaFail[ep.BaseURL]) < catalogFailTTL
	s.metaMu.Unlock()
	if failed {
		return nil
	}
	ctx, cancel := context.WithTimeout(s.base, 5*time.Second)
	infos, err := provider.New(ep.BaseURL, ep.APIKey).ModelInfo(ctx)
	cancel()
	if err != nil {
		s.metaMu.Lock()
		s.metaFail[ep.BaseURL] = time.Now()
		s.metaMu.Unlock()
		return nil
	}
	for i := range infos {
		infos[i].Status = lowerStatus(infos[i].Status)
	}
	s.refreshMeta(ep, infos)
	var names []string
	for _, info := range infos {
		if info.ID == "" || (info.Status != "" && info.Status != "serving") || s.cfg.WorkerModelHidden(info.ID) {
			continue
		}
		names = append(names, info.ID)
	}
	sort.Strings(names)
	return names
}

// modelsNoteText lists the models a worker may be set to, for a chat's
// fiona: every name, its description and window, and the default. "" when
// there is no name at all.
func (s *Server) modelsNoteText() string {
	names := s.workerModels()
	if len(names) == 0 {
		return ""
	}
	var meta map[string]metaEntry
	if ep, err := s.cfg.Endpoint(""); err == nil {
		s.metaMu.Lock()
		meta = s.meta[ep.BaseURL]
		s.metaMu.Unlock()
	}
	items := make([]string, 0, len(names))
	for _, n := range names {
		var detail []string
		if d := s.cfg.RouteDescription(n); d != "" {
			detail = append(detail, d)
		}
		window := s.cfg.RouteContextLength(n)
		if window <= 0 {
			window = meta[n].context
		}
		if w := windowText(window); w != "" {
			detail = append(detail, w+" context")
		}
		if len(detail) > 0 {
			n += " (" + strings.Join(detail, "; ") + ")"
		}
		items = append(items, n)
	}
	return "[Models for relay send's model, up now: " + strings.Join(items, "; ") + "; default is " + s.cfg.Model.Default + ".]"
}

// modelsNote is the note a chat's turn carries: the models list on its first
// turn after the daemon starts and again whenever it changes, "" otherwise.
// It rides the turn's user message (never the system prompt), so the cached
// transcript only ever grows.
func (s *Server) modelsNote(stamp string) string {
	note := s.modelsNoteText()
	if note == "" {
		return ""
	}
	if last, _ := s.noted.Load(stamp); last == note {
		return ""
	}
	s.noted.Store(stamp, note)
	return note
}

// workerModel resolves the model a relay send asks for: "default" is
// model.default; a name that is not offered to workers (not live, hidden by
// relay.hidden_models, or unknown) is an error listing those that are.
func (s *Server) workerModel(name string) (string, error) {
	if name == "default" {
		return s.cfg.Model.Default, nil
	}
	names := s.workerModels()
	for _, n := range names {
		if n == name {
			return name, nil
		}
	}
	if len(names) == 0 {
		return "", fmt.Errorf("unknown model %s; no model can be offered to workers right now (leave model out, or use default)", name)
	}
	return "", fmt.Errorf("unknown model %s; known: %s", name, strings.Join(names, ", "))
}
