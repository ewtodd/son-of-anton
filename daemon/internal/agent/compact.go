package agent

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Compaction configures a Runner's compaction.
type Compaction struct {
	Enabled       bool             // automatic compaction; manual always works
	Threshold     float64          // of the context window; 0 means 0.75
	ContextLength int              // the model's window; 0 means 256000
	Client        *provider.Client // the summary model; nil means the session's
	Model         string           // "" means the session's model
}

// CompactResult is a finished compaction.
type CompactResult struct {
	ThroughSeq   int64
	Summary      string
	BeforeTokens int
}

// CompactPlan is what a manual compaction would summarize.
type CompactPlan struct {
	Rows       int
	ThroughSeq int64
	Tokens     int
}

// ErrNothingToCompact means the rows are all in the kept tail.
var ErrNothingToCompact = errors.New("agent: nothing to compact")

const (
	defaultContextLength = 256000
	defaultThreshold     = 0.75
	minTailRows          = 2
	summaryCooldown      = 60 * time.Second // CC:4608
)

var thinkRE = regexp.MustCompile(`(?is)<think>.*?</think>|<thinking>.*?</thinking>|<reasoning>.*?</reasoning>`)

// compState is a session's compaction state; guarded by Session.mu.
type compState struct {
	ctxLen     int
	lastPrompt int // the latest response's prompt_tokens
	autoOff    bool
	cooldown   time.Time
	afterAuto  bool // the next response judges the last automatic compaction
	compacting bool
	client     *provider.Client
	model      string
}

func (s *Session) threshold() float64 {
	if t := s.runner.Compaction.Threshold; t > 0 {
		return t
	}
	return defaultThreshold
}

// SetContextLength sets the model's window from the next request; 0 means
// the default.
func (s *Session) SetContextLength(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.comp.ctxLen = n
}

// ContextLength is the model's window as the session knows it.
func (s *Session) ContextLength() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.contextLengthLocked()
}

func (s *Session) contextLengthLocked() int {
	if s.comp.ctxLen > 0 {
		return s.comp.ctxLen
	}
	return defaultContextLength
}

// SetSummarizer changes the summary model from the next compaction; a nil
// client or empty model means the session's own.
func (s *Session) SetSummarizer(c *provider.Client, model string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.comp.client, s.comp.model = c, model
}

// estimateTokens is the characters / 4 estimate, content plus tool calls.
func estimateTokens(rows []store.Message) int {
	n := 0
	for _, m := range rows {
		n += rowChars(m)
	}
	return n / 4
}

func rowChars(m store.Message) int {
	return utf8.RuneCountInString(m.Content) + len(m.ToolCalls)
}

// tailStart is the index in rows where the kept tail begins. budgetChars < 0
// keeps only the floor (the last 2 rows); otherwise the longest suffix that
// fits. A tail never starts on a tool row: it starts at the assistant row
// that made the calls.
func tailStart(rows []store.Message, budgetChars int) int {
	start := len(rows) - minTailRows
	if budgetChars >= 0 {
		total, i := 0, len(rows)
		for i > 0 && total+rowChars(rows[i-1]) <= budgetChars {
			total += rowChars(rows[i-1])
			i--
		}
		start = min(i, start)
	}
	start = max(start, 0)
	for start > 0 && rows[start].Role == "tool" {
		start--
	}
	return start
}

// plan loads the candidates after the last compaction and splits them.
func (s *Session) plan(ctx context.Context, auto bool) (head []store.Message, previous string, err error) {
	stored, err := s.runner.Store.Messages(ctx, s.id)
	if err != nil {
		return nil, "", err
	}
	comps, err := s.runner.Store.Compactions(ctx, s.id)
	if err != nil {
		return nil, "", err
	}
	rows := stored
	if n := len(comps); n > 0 {
		previous = comps[n-1].Summary
		rows = rowsAfter(stored, comps[n-1].ThroughSeq)
	}
	budget := -1
	if auto {
		budget = int(0.2*s.threshold()*float64(s.ContextLength())) * 4
	}
	return rows[:tailStart(rows, budget)], previous, nil
}

func rowsAfter(rows []store.Message, seq int64) []store.Message {
	for i, m := range rows {
		if m.Seq > seq {
			return rows[i:]
		}
	}
	return nil
}

// PlanCompaction says what a manual Compact would summarize.
func (s *Session) PlanCompaction() (CompactPlan, error) {
	head, _, err := s.plan(context.Background(), false)
	if err != nil {
		return CompactPlan{}, err
	}
	if len(head) == 0 {
		return CompactPlan{}, ErrNothingToCompact
	}
	return CompactPlan{Rows: len(head), ThroughSeq: head[len(head)-1].Seq, Tokens: estimateTokens(head)}, nil
}

// Compact summarizes the head of the transcript now, keeping only the last
// rows verbatim. focus, when set, is the topic the summary should favor. It
// fails while a turn is running.
func (s *Session) Compact(ctx context.Context, focus string) (CompactResult, error) {
	s.mu.Lock()
	if s.turn != nil || s.comp.compacting {
		s.mu.Unlock()
		return CompactResult{}, errors.New("agent: a turn or compaction is already running")
	}
	s.comp.compacting = true
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.comp.compacting = false
		s.mu.Unlock()
	}()
	return s.compact(ctx, strings.TrimSpace(focus), false, nil)
}

// compact summarizes the head. auto uses the tail budget, otherwise only the
// floor. emit, when set, announces the compaction.
func (s *Session) compact(ctx context.Context, focus string, auto bool, emit func(Event)) (CompactResult, error) {
	head, previous, err := s.plan(ctx, auto)
	if err != nil {
		return CompactResult{}, err
	}
	if len(head) == 0 {
		return CompactResult{}, ErrNothingToCompact
	}
	s.mu.Lock()
	client, model := s.comp.client, s.comp.model
	if client == nil {
		client = s.client
	}
	if model == "" {
		model = s.model
	}
	ctxLen, before := s.contextLengthLocked(), s.comp.lastPrompt
	s.mu.Unlock()
	if before <= 0 {
		before = utf8.RuneCountInString(s.promptText())/4 + estimateTokens(head)
	}

	resp, err := client.Complete(ctx, provider.Request{
		Model:    model,
		Messages: []provider.Message{{Role: "user", Content: summaryPrompt(head, previous, focus, ctxLen)}},
	}, nil)
	if err != nil {
		return CompactResult{}, fmt.Errorf("summary request: %w", err)
	}
	summary := strings.TrimSpace(thinkRE.ReplaceAllString(resp.Message.Content, ""))
	if summary == "" {
		return CompactResult{}, errors.New("the summary model returned nothing")
	}

	st := s.runner.Store
	rec, err := st.Session(ctx, s.id)
	if err != nil {
		return CompactResult{}, err
	}
	text, err := s.runner.Prompt(ctx, rec)
	if err != nil {
		return CompactResult{}, err
	}
	hash, err := st.PutPrompt(ctx, text)
	if err != nil {
		return CompactResult{}, err
	}
	res := CompactResult{ThroughSeq: head[len(head)-1].Seq, Summary: summary, BeforeTokens: before}
	if err := st.AddCompaction(ctx, s.id, store.Compaction{ThroughSeq: res.ThroughSeq, Summary: summary, BeforeTokens: before}, hash); err != nil {
		return CompactResult{}, err
	}
	s.mu.Lock()
	s.prompt = text
	s.comp.lastPrompt = 0
	s.mu.Unlock()
	if emit != nil {
		emit(Event{Kind: "compaction", Compaction: &res})
	}
	return res, nil
}

// promptText is the session's current system prompt.
func (s *Session) promptText() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.prompt
}

// autoDue says whether the last response's prompt size calls for automatic
// compaction before the next request.
func (s *Session) autoDue() bool {
	c := s.runner.Compaction
	s.mu.Lock()
	defer s.mu.Unlock()
	if !c.Enabled || s.comp.autoOff || time.Now().Before(s.comp.cooldown) || s.comp.lastPrompt <= 0 {
		return false
	}
	return float64(s.comp.lastPrompt) >= s.threshold()*float64(s.contextLengthLocked())
}

// noteResponse records a response's prompt size. The first response after an
// automatic compaction that is still over the threshold turns automatic
// compaction off, and says so.
func (s *Session) noteResponse(promptTokens int, emit func(Event)) {
	s.mu.Lock()
	s.comp.lastPrompt = promptTokens
	warn := s.comp.afterAuto && float64(promptTokens) >= s.threshold()*float64(s.contextLengthLocked())
	s.comp.afterAuto = false
	if warn {
		s.comp.autoOff = true
	}
	s.mu.Unlock()
	if warn {
		emit(Event{Kind: "warning", Text: "Compaction did not bring the context below the threshold, so automatic compaction is off for this session. " +
			"Run /compact with a focus topic to summarize harder, or start a new session."})
	}
}

// autoCompact is the compaction before a request. Failing to summarize
// leaves the transcript alone, warns, and backs automatic compaction off for
// a minute.
func (s *Session) autoCompact(ctx context.Context, emit func(Event)) bool {
	_, err := s.compact(ctx, "", true, emit)
	switch {
	case err == nil:
		s.mu.Lock()
		s.comp.afterAuto = true
		s.mu.Unlock()
		return true
	case errors.Is(err, ErrNothingToCompact), ctx.Err() != nil:
	default:
		s.mu.Lock()
		s.comp.cooldown = time.Now().Add(summaryCooldown)
		s.mu.Unlock()
		emit(Event{Kind: "warning", Text: "Compaction failed, continuing without it: " + err.Error()})
	}
	return false
}

// overflowCompact answers a context-overflow error: learn the stated limit,
// then compact regardless of the cooldown and the off switch.
func (s *Session) overflowCompact(ctx context.Context, err error, emit func(Event)) bool {
	limit, ok := provider.ContextOverflow(err)
	if !ok {
		return false
	}
	if limit > 0 {
		s.SetContextLength(limit)
	}
	_, cerr := s.compact(ctx, "", true, emit)
	return cerr == nil
}
