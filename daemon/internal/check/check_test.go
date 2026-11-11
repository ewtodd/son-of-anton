package check_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/check"
)

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
	want := check.NotePrefix + check.Questions[1].Nudge + " " + check.Questions[2].Nudge
	if note != want || len(flags) != 2 || !strings.HasPrefix(flags[0], "unchecked 0.95") {
		t.Fatalf("note %q flags %v; claim 0.6 is under its 0.73, unchecked over its 0.59, offtopic at its 0.44 (the cut-offs were chosen as p >= cut-off)", note, flags)
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
	if err != nil || len(*asked) != 1 || strings.Join((*asked)[0], ",") != "claim" || note != check.NotePrefix+check.Recheck.Nudge {
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
