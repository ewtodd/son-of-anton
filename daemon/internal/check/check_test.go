package check_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/check"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

func TestConfigFromReadsTheCheckerTable(t *testing.T) {
	home := t.TempDir()
	toml := "[checker]\nbase_url = \"http://fiona:8084/v1/\"\nmodels = [\"eklow/\"]\nquestions = [\"claim\", \"unchecked\"]\n" +
		"max_retries = 1\nlog_only = true\n[checker.cutoffs]\nclaim = 0.7342\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(toml), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := config.Load(home)
	if err != nil {
		t.Fatal(err)
	}
	cfg, ok := check.ConfigFrom(c)
	if !ok || cfg.BaseURL != "http://fiona:8084/v1" || strings.Join(cfg.Questions, ",") != "claim,unchecked" ||
		cfg.MaxRetries != 1 || !cfg.LogOnly || cfg.Cutoffs["claim"] != 0.7342 || cfg.Cutoffs["offtopic"] != 0.5 || cfg.Timeout != 3*time.Second {
		t.Fatalf("ConfigFrom = %+v, %v", cfg, ok)
	}
	if _, ok := check.ConfigFrom(&config.Config{}); ok {
		t.Fatal("no [checker] table, no checker")
	}
}

var ctx = context.Background()

// The state the replay's checker.py context() builds for the same turn (its
// SHA-256): the heads were fitted on that text, so it must not drift. Two
// calls in one step (both calls, then both results), text over each cut-off,
// and characters outside ASCII (counted in code points, as Python does).
func TestStateIsTheTextTheHeadsWereFittedOn(t *testing.T) {
	in := check.Input{
		Tools: []string{"cronjob", "read_file", "relay"},
		User:  "[Mon 2026-11-09 10:00 CST] did the backup run? " + strings.Repeat("é", 3005),
		Steps: []check.Step{{
			Calls: []check.Call{{Name: "cronjob", Arguments: `{"action":"list"}`},
				{Name: "read_file", Arguments: `{"path":"` + strings.Repeat("x", 700) + `"}`}},
			Results: []string{"jobs: backup nightly 02:00", strings.Repeat("y", 1600)},
		}},
		Draft: "It ran at 02:00. ✓",
	}
	s := check.State(in)
	sum := sha256.Sum256([]byte(s))
	if got := hex.EncodeToString(sum[:]); got != "653ea77664bc82e56c1974b5472884c7584fce2e40c4cf4117ab19631837d098" || len([]rune(s)) != 5441 {
		t.Fatalf("State differs from checker.py's context(): sha256 %s, %d characters\n%.400s", got, len([]rune(s)), s)
	}
	if !strings.Contains(s, "before the draft:\ncall cronjob {\"action\":\"list\"}\ncall read_file") ||
		!strings.HasSuffix(s, "it makes no tool call):\nIt ran at 02:00. ✓") {
		t.Fatalf("state = %.400s", s)
	}
	if !strings.Contains(check.State(check.Input{Draft: "d"}), "before the draft:\n(none)\n\n") {
		t.Fatal("a turn with no tool steps says (none)")
	}
}

// server is a fake System One endpoint answering each question with ps[id];
// asked records the question ids of every request.
func server(t *testing.T, ps map[string]float64, status int) (*httptest.Server, *[][]string) {
	t.Helper()
	var asked [][]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/systemone" {
			http.NotFound(w, r)
			return
		}
		var req struct {
			State     string                     `json:"state"`
			Questions map[string]json.RawMessage `json:"questions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		var ids []string
		answers := map[string]any{}
		for id := range req.Questions {
			ids = append(ids, id)
			answers[id] = map[string]any{"noul": ps[id]}
		}
		slices.Sort(ids)
		asked = append(asked, ids)
		if status != 0 {
			w.WriteHeader(status)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"answers": answers})
	}))
	t.Cleanup(srv.Close)
	return srv, &asked
}

func checker(url string) *check.Checker {
	return check.New(check.Config{BaseURL: url + "/v1", Models: []string{"eklow/"}, Timeout: time.Second,
		Cutoffs:       map[string]float64{"claim": 0.73, "unchecked": 0.59, "offtopic": 0.44},
		RecheckCutoff: 0.5, MaxRetries: 2})
}

func TestTheFirstDraftGetsEveryQuestionAtItsCutoff(t *testing.T) {
	srv, asked := server(t, map[string]float64{"claim": 0.6, "unchecked": 0.95, "offtopic": 0.44}, 0)
	note, flags, err := checker(srv.URL).Review(ctx, check.Input{Draft: "d"}, 0)
	if err != nil || len(*asked) != 1 || len((*asked)[0]) != 3 {
		t.Fatalf("err %v, asked %v", err, *asked)
	}
	want := check.NotePrefix + check.Questions[1].Nudge + " " + check.Questions[2].Nudge + check.NoteSuffix
	if note != want || len(flags) != 2 || !strings.HasPrefix(flags[0], "unchecked 0.95") {
		t.Fatalf("note %q flags %v; claim 0.6 is under its 0.73, unchecked over its 0.59, offtopic at its 0.44 (the cut-offs were chosen as p >= cut-off)", note, flags)
	}
}

// The question's premise is a turn without a tool call that checked the state;
// once the turn has made one, the head cannot tell which state it checked.
func TestUncheckedIsAskedOnlyOfATurnWithoutToolCalls(t *testing.T) {
	srv, asked := server(t, map[string]float64{"unchecked": 0.93}, 0)
	k := checker(srv.URL)
	logged := check.Input{Steps: []check.Step{{Calls: []check.Call{{Name: "write_file", Arguments: "{}"}}, Results: []string{"ok"}}},
		Draft: "Logged it."}
	if note, _, err := k.Review(ctx, logged, 0); note != "" || err != nil || slices.Contains((*asked)[0], "unchecked") {
		t.Fatalf("note %q err %v asked %v; a turn that called a tool is not asked unchecked", note, err, *asked)
	}
	if note, _, _ := k.Review(ctx, check.Input{Draft: "It ran."}, 0); !strings.Contains(note, check.Questions[1].Nudge) {
		t.Fatalf("note %q asked %v; a turn without tool calls still is", note, *asked)
	}
}

func TestQuestionsLimitWhatADraftIsAsked(t *testing.T) {
	srv, asked := server(t, map[string]float64{"claim": 0.9, "unchecked": 0.9, "offtopic": 0.9}, 0)
	cfg := check.Config{BaseURL: srv.URL + "/v1", Timeout: time.Second, MaxRetries: 2, RecheckCutoff: 0.5,
		Cutoffs: map[string]float64{"claim": 0.5, "unchecked": 0.5, "offtopic": 0.5}, Questions: []string{"claim", "unchecked"}}
	if _, flags, _ := check.New(cfg).Review(ctx, check.Input{Draft: "d"}, 0); strings.Join((*asked)[0], ",") != "claim,unchecked" || len(flags) != 2 {
		t.Fatalf("asked %v flags %v; offtopic is off", *asked, flags)
	}
	cfg.Questions = []string{"unchecked"}
	k := check.New(cfg)
	if note, _, _ := k.Review(ctx, check.Input{Draft: "d"}, 1); note != "" || len(*asked) != 1 {
		t.Fatalf("note %q asked %v; without claim a retry is not rechecked", note, *asked)
	}
	if note, _, _ := k.Review(ctx, check.Input{Steps: []check.Step{{}}, Draft: "d"}, 0); note != "" || len(*asked) != 1 {
		t.Fatalf("note %q asked %v; nothing left to ask, nothing is sent", note, *asked)
	}
}

func TestAnAnswerMissingAQuestionIsAFailure(t *testing.T) {
	for _, answers := range []string{`"unchecked":{"noul":null},"offtopic":{"noul":0.1}`, `"unchecked":{"noul":0.1}`} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"answers":{"claim":{"noul":0.9},` + answers + `}}`))
		}))
		note, _, err := checker(srv.URL).Review(ctx, check.Input{Draft: "d"}, 0)
		srv.Close()
		if note != "" || err == nil {
			t.Fatalf("answers {%s}: note %q err %v; a question left unanswered fails the check, so the draft stands", answers, note, err)
		}
	}
}

func TestARetryIsAskedOnlyAboutClaims(t *testing.T) {
	srv, asked := server(t, map[string]float64{"claim": 0.6, "unchecked": 0.99}, 0)
	k := checker(srv.URL)
	note, _, err := k.Review(ctx, check.Input{Draft: "d"}, 1)
	if err != nil || len(*asked) != 1 || strings.Join((*asked)[0], ",") != "claim" || note != check.NotePrefix+check.Recheck.Nudge+check.NoteSuffix {
		t.Fatalf("note %q err %v asked %v; 0.6 is over the recheck cut-off 0.5", note, err, *asked)
	}
	if note, _, _ := k.Review(ctx, check.Input{Draft: "d"}, 2); note != "" || len(*asked) != 1 {
		t.Fatal("past max_retries the draft stands, unasked")
	}
}

func TestACheckerThatCannotAnswerLetsTheDraftStand(t *testing.T) {
	srv, _ := server(t, nil, http.StatusServiceUnavailable)
	if note, _, err := checker(srv.URL).Review(ctx, check.Input{Draft: "d"}, 0); note != "" || err == nil {
		t.Fatalf("note %q err %v", note, err)
	}
	release := make(chan struct{})
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-time.After(5 * time.Second):
		}
	}))
	t.Cleanup(slow.Close)
	t.Cleanup(func() { close(release) })
	k := check.New(check.Config{BaseURL: slow.URL + "/v1", Timeout: 50 * time.Millisecond, MaxRetries: 2})
	start := time.Now()
	if note, _, err := k.Review(ctx, check.Input{Draft: "d"}, 0); note != "" || err == nil || time.Since(start) > time.Second {
		t.Fatalf("note %q err %v after %v; the timeout bounds the wait", note, err, time.Since(start))
	}
}

func TestAppliesByServedModelPrefix(t *testing.T) {
	c := check.Config{Models: []string{"eklow/nemotron", ""}}
	if !c.Applies("eklow/nemotron-3.5-bolt-e4b") || c.Applies("radiance/qwen3.8-27b") || c.Applies("") {
		t.Fatal("prefixes of the served model; an empty prefix matches nothing")
	}
}

func TestLogOnlyAsksEveryQuestionRecordsAndSendsNothingBack(t *testing.T) {
	srv, asked := server(t, map[string]float64{"claim": 0.9, "unchecked": 0.95, "offtopic": 0.1}, 0)
	path := filepath.Join(t.TempDir(), "checker", "checks.jsonl")
	k := check.New(check.Config{BaseURL: srv.URL + "/v1", Timeout: time.Second, MaxRetries: 2, RecheckCutoff: 0.5,
		Cutoffs: map[string]float64{"claim": 0.7, "unchecked": 0.5, "offtopic": 0.4}, LogOnly: true, RecordPath: path})
	in := check.Input{Steps: []check.Step{{Calls: []check.Call{{Name: "write_file"}}, Results: []string{"ok"}}},
		Draft: "I saved it.", Chat: "signal:group:gW", Model: "eklow/bolt"}
	note, flags, err := k.Review(ctx, in, 0)
	if err != nil || note != "" || strings.Join(flags, ",") != "claim 0.90" || strings.Join((*asked)[0], ",") != "claim,offtopic,unchecked" {
		t.Fatalf("note %q flags %v err %v asked %v; log_only asks all three, even unchecked after a tool call, sends nothing "+
			"back, and flags only what a live checker would have sent back (not unchecked after a tool call)", note, flags, err, *asked)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var rec check.Record
	if lines := strings.Split(strings.TrimSpace(string(b)), "\n"); len(lines) != 1 || json.Unmarshal([]byte(lines[0]), &rec) != nil {
		t.Fatalf("record file = %q", b)
	}
	if rec.Chat != "signal:group:gW" || rec.Model != "eklow/bolt" || rec.State != check.State(in) || rec.SentBack ||
		rec.Answers["unchecked"] != 0.95 || rec.Cutoffs["claim"] != 0.7 || rec.Cutoffs["unchecked"] != 0.5 || len(rec.Flags) != 1 {
		t.Fatalf("record = %+v", rec)
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o600 {
		t.Fatalf("record file mode %v; it holds chats", st.Mode().Perm())
	}
}

func TestASentBackCheckIsRecordedAsSuch(t *testing.T) {
	srv, _ := server(t, map[string]float64{"claim": 0.9, "unchecked": 0.1, "offtopic": 0.1}, 0)
	path := filepath.Join(t.TempDir(), "checks.jsonl")
	k := check.New(check.Config{BaseURL: srv.URL + "/v1", Timeout: time.Second, MaxRetries: 2, RecheckCutoff: 0.5,
		Cutoffs: map[string]float64{"claim": 0.7, "unchecked": 0.5, "offtopic": 0.4}, RecordPath: path})
	note, _, _ := k.Review(ctx, check.Input{Draft: "d"}, 0)
	k.Review(ctx, check.Input{Draft: "d2"}, 1)
	b, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	var first, second check.Record
	if note == "" || len(lines) != 2 || json.Unmarshal([]byte(lines[0]), &first) != nil || json.Unmarshal([]byte(lines[1]), &second) != nil ||
		!first.SentBack || first.Attempt != 0 || second.Attempt != 1 || len(second.Answers) != 1 {
		t.Fatalf("note %q records %q", note, b)
	}
}
