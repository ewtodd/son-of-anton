package agent_test

// Phase 2a: what the daemon needs from a session. A turn can be stopped with
// or without letting running tools finish, and every ending closes on an
// assistant row; committed rows are announced with their seq; the model and
// permission mode change for the next request; background processes can be
// counted, killed, and their completion notices observed.

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// sendAsync runs Send in a goroutine, forwarding events, and returns a
// channel with its error.
func sendAsync(s *agent.Session, text string, onEvent func(agent.Event)) <-chan error {
	done := make(chan error, 1)
	go func() {
		_, err := s.Send(ctx, text, onEvent)
		done <- err
	}()
	return done
}

func wait(t *testing.T, done <-chan error) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(10 * time.Second):
		t.Fatal("the turn did not end")
		return nil
	}
}

// signalOn returns an onEvent that closes ch the first time an event
// matching pred arrives. Stop is never called from inside onEvent.
func signalOn(pred func(agent.Event) bool) (func(agent.Event), <-chan struct{}) {
	ch := make(chan struct{})
	var once sync.Once
	return func(e agent.Event) {
		if pred(e) {
			once.Do(func() { close(ch) })
		}
	}, ch
}

func TestAStopKeepsWhatStreamed(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{Content: "partial answer", ChunkRunes: 8, StallAfterFirst: true},
		fakellm.Turn{Content: "ok"},
	)
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	onEvent, streamed := signalOn(func(e agent.Event) bool { return e.Kind == "assistant.delta" })
	done := sendAsync(s, "q", onEvent)
	<-streamed
	s.Stop(false)
	if err := wait(t, done); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped", err)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 2 || stored[0].Content != "q" || stored[1].Role != "assistant" || stored[1].Content != "partial" {
		t.Fatalf("stored %+v; a stopped request keeps what it streamed (trimmed) as the closing row", stored)
	}
	send(t, s, "next")
	ms := full(t, fake.Messages(1))
	if roles(ms) != "system,user,assistant,user" || ms[2].Content != "partial" {
		t.Fatalf("after a stop: %s %+v", roles(ms), ms)
	}
	checkPairing(t, ms)
}

func TestStopFinishingToolsKeepsTheirResults(t *testing.T) {
	st := newStore(t)
	slow := &fakeTool{name: "slow_tool", delay: 300 * time.Millisecond}
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "slow_tool", `{}`), call("c2", "echo_tool", `{}`)}},
	)
	s, _ := toolRunner(t, st, fake, slow, echo).NewSession(ctx, "interactive", t.TempDir())
	onEvent, started := signalOn(func(e agent.Event) bool { return e.Kind == "tool.start" && e.CallID == "c1" })
	done := sendAsync(s, "go", onEvent)
	<-started
	s.Stop(true)
	if err := wait(t, done); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped", err)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 5 {
		t.Fatalf("stored %+v, want user, assistant, two tool rows, closing row", stored)
	}
	if strings.Contains(stored[2].Content, "interrupted") || !strings.Contains(stored[2].Content, "echo") {
		t.Fatalf("the running call must finish with its real result, got %q", stored[2].Content)
	}
	if !strings.Contains(strings.ToLower(stored[3].Content), "cancel") || echo.runs.Load() != 0 {
		t.Fatalf("a call that had not started is cancelled, got %q (runs %d)", stored[3].Content, echo.runs.Load())
	}
	if stored[4].Role != "assistant" || stored[4].Content != agent.InterruptedText {
		t.Fatalf("closing row = %+v", stored[4])
	}
}

func TestStopWithoutFinishingToolsCancelsThem(t *testing.T) {
	st := newStore(t)
	slow := &fakeTool{name: "slow_tool", delay: 30 * time.Second}
	fake := fakellm.New(t, fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "slow_tool", `{}`)}})
	s, _ := toolRunner(t, st, fake, slow).NewSession(ctx, "interactive", t.TempDir())
	onEvent, started := signalOn(func(e agent.Event) bool { return e.Kind == "tool.start" })
	start := time.Now()
	done := sendAsync(s, "go", onEvent)
	<-started
	s.Stop(false)
	if err := wait(t, done); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("Stop(false) must cancel the running tool")
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 4 || !strings.Contains(stored[2].Content, "interrupted") || stored[3].Content != agent.InterruptedText {
		t.Fatalf("stored %+v", stored)
	}
}

// The daemon runs Send on its own goroutine; a steer can land before Send
// has registered the turn. Begin reserves the turn so that stop is kept.
func TestAStopBetweenBeginAndSendIsNotLost(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	s.Begin()
	s.Stop(true)
	if err := wait(t, sendAsync(s, "q", nil)); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped: a stop after Begin applies to the next Send", err)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 2 || stored[0].Content != "q" || stored[1].Content != agent.InterruptedText {
		t.Fatalf("stored %+v", stored)
	}
}

func TestStopWhenIdleIsANoOp(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "fine"})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	s.Stop(true)
	s.Stop(false)
	if res := send(t, s, "hello"); res.Content != "fine" {
		t.Fatalf("a stop while idle must not carry over into the next turn: %+v", res)
	}
}

func TestCommittedRowsAreAnnouncedWithTheirSeq(t *testing.T) {
	st := newStore(t)
	slow := &fakeTool{name: "slow_tool", delay: 20 * time.Millisecond}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "slow_tool", `{}`)}},
		fakellm.Turn{Content: "done"},
	)
	s, _ := toolRunner(t, st, fake, slow).NewSession(ctx, "interactive", t.TempDir())
	var mu sync.Mutex
	var events []agent.Event
	if _, err := s.Send(ctx, "go", func(e agent.Event) {
		mu.Lock()
		events = append(events, e)
		mu.Unlock()
	}); err != nil {
		t.Fatal(err)
	}
	stored, _ := st.Messages(ctx, s.ID())
	var announced []int64
	toolEnd, toolRow, secondDelta := -1, -1, -1
	for i, e := range events {
		switch e.Kind {
		case "message":
			if e.Message == nil {
				t.Fatalf("event %d: a message event carries the stored row", i)
			}
			announced = append(announced, e.Message.Seq)
			if e.Message.Role == "tool" {
				toolRow = i
			}
		case "tool.end":
			toolEnd = i
			if e.Duration < 20*time.Millisecond {
				t.Fatalf("tool.end Duration = %v, want at least the tool's 20ms", e.Duration)
			}
		case "assistant.delta":
			secondDelta = i
		}
	}
	if len(announced) != len(stored) {
		t.Fatalf("announced seqs %v for %d stored rows", announced, len(stored))
	}
	for i, m := range stored {
		if announced[i] != m.Seq {
			t.Fatalf("announced seqs %v, stored %+v: every committed row is announced once, in order", announced, stored)
		}
	}
	if toolEnd < 0 || toolRow < toolEnd || secondDelta < toolRow {
		t.Fatalf("order: tool.end at %d, tool row announced at %d, next step's delta at %d", toolEnd, toolRow, secondDelta)
	}
}

func TestSetModelAppliesToTheNextTurn(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"})
	other := fakellm.New(t, fakellm.Turn{Content: "two"})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	send(t, s, "1")
	c := provider.New(other.URL, "k")
	s.SetModel(c, "m2", "low")
	send(t, s, "2")
	req := other.Request(0)
	if req["model"] != "m2" || req["reasoning_effort"] != "low" {
		t.Fatalf("after SetModel the request has model %v, effort %v", req["model"], req["reasoning_effort"])
	}
	if string(other.Messages(0)[0]) != string(fake.Messages(0)[0]) {
		t.Fatal("changing the model must not change the system prompt bytes")
	}
}

func TestSetModeAppliesToTheNextCommand(t *testing.T) {
	st := newStore(t)
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"chmod 777 f.txt"}`)}},
		fakellm.Turn{Content: "blocked"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "Bash", `{"command":"chmod 777 f.txt"}`)}},
		fakellm.Turn{Content: "ran"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	r.Mode = approval.Default // nobody to ask: dangerous commands are denied
	s, _ := r.NewSession(ctx, "interactive", cwd)
	defer s.Close()
	send(t, s, "try")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o644 {
		t.Fatal("in default mode with no approver the command must not run")
	}
	s.SetMode(approval.Yolo)
	send(t, s, "again")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("after SetMode(Yolo) the command runs")
	}
}

func TestBackgroundProcessesCanBeCountedAndKilled(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"sleep 30","run_in_background":true}`)}},
		fakellm.Turn{Content: "started"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "start")
	if n := s.BackgroundRunning(); n != 1 {
		t.Fatalf("BackgroundRunning = %d, want 1", n)
	}
	stored, _ := st.Messages(ctx, s.ID())
	pid := int(storedResult(t, stored[2].Content)["pid"].(float64))
	if n := s.KillBackground(); n != 1 {
		t.Fatalf("KillBackground = %d, want 1", n)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && (s.BackgroundRunning() != 0 || syscall.Kill(pid, 0) == nil) {
		time.Sleep(50 * time.Millisecond)
	}
	if s.BackgroundRunning() != 0 || syscall.Kill(pid, 0) == nil {
		t.Fatal("KillBackground must kill the process and BackgroundRunning must drop to 0")
	}
}

func TestOnNotifyFiresWhenANoticeIsQueued(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"sleep 0.2; echo bg-done","run_in_background":true}`)}},
		fakellm.Turn{Content: "ok"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	r.AsyncDelivery = true
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	var fired atomic.Int32
	s.OnNotify(func() { fired.Add(1) })
	send(t, s, "go")
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && fired.Load() == 0 {
		time.Sleep(20 * time.Millisecond)
	}
	if fired.Load() == 0 {
		t.Fatal("OnNotify's callback must run when a completion notice is queued")
	}
	if got := s.TakeNotifications(); len(got) != 1 || !strings.Contains(got[0], "bg-done") {
		t.Fatalf("notifications = %q", got)
	}
}
