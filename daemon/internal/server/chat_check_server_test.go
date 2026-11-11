package server_test

// The gateway's end-of-turn checker ([checker]): a reply served by one of its
// models is reviewed before it is sent; a flagged draft never reaches the chat.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/check"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

// systemOne is a fake checker endpoint that says yes to "claim" on its first
// request only.
func systemOne(t *testing.T) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Questions map[string]json.RawMessage `json:"questions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		yes := 0.0
		if n.Add(1) == 1 {
			yes = 0.9
		}
		answers := map[string]any{}
		for id := range req.Questions {
			p := 0.0
			if id == "claim" {
				p = yes
			}
			answers[id] = map[string]any{"noul": p}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"answers": answers})
	}))
	t.Cleanup(srv.Close)
	return srv, &n
}

func checkerConfig(url string) string {
	return "[checker]\nbase_url = \"" + url + "/v1\"\nmodels = [\"eklow/\"]\n"
}

func TestTheGatewaySendsBackAFlaggedDraftBeforeTheChatSeesIt(t *testing.T) {
	srv, asked := systemOne(t)
	fake := fakellm.New(t,
		fakellm.Turn{Content: "I've logged it.", ServedModel: "eklow/bolt"},
		fakellm.Turn{Content: "I have not logged it yet.", ServedModel: "eklow/bolt"},
	)
	r := newChatRig(t, fake, checkerConfig(srv.URL))
	r.say("log my weight")
	r.reply(sigGroup, "I have not logged it yet.")
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "I've logged it.") {
			t.Fatalf("the flagged draft reached the chat: %+v", m)
		}
	}
	if asked.Load() != 2 {
		t.Fatalf("checker asked %d times; the draft, then the retry", asked.Load())
	}
	msgs := fake.Messages(1)
	if last := string(msgs[len(msgs)-1]); !strings.Contains(last, check.NotePrefix) {
		t.Fatalf("the retry request ends with %s; the checker's note", last)
	}
	r.sig.Push(signalfake.OwnerInGroup(sigAccount, sigGroup, "I have not logged it yet.", r.ts))
}

func TestTheGatewaySendsTheReplyWhenTheCheckerIsDown(t *testing.T) {
	srv, _ := systemOne(t)
	srv.Close()
	fake := fakellm.New(t, fakellm.Turn{Content: "I've logged it.", ServedModel: "eklow/bolt"})
	r := newChatRig(t, fake, checkerConfig(srv.URL))
	r.say("log my weight")
	r.reply(sigGroup, "I've logged it.")
	if fake.Count() != 1 {
		t.Fatalf("%d requests; a checker that cannot answer lets the reply stand", fake.Count())
	}
}

func TestTheGatewayLeavesOtherModelsUnchecked(t *testing.T) {
	srv, asked := systemOne(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "Done.", ServedModel: "radiance/qwen3.8-27b"})
	r := newChatRig(t, fake, checkerConfig(srv.URL))
	r.say("hello")
	r.reply(sigGroup, "Done.")
	if asked.Load() != 0 {
		t.Fatalf("checker asked %d times about a model outside [checker] models", asked.Load())
	}
}

func TestALogOnlyGatewayRecordsAndLetsTheDraftThrough(t *testing.T) {
	srv, asked := systemOne(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "I've logged it.", ServedModel: "eklow/bolt"})
	r := newChatRig(t, fake, checkerConfig(srv.URL)+"log_only = true\nrecord = true\n")
	r.say("log my weight")
	r.reply(sigGroup, "I've logged it.")
	if fake.Count() != 1 || asked.Load() != 1 {
		t.Fatalf("%d model requests, %d checks; log_only checks once and sends nothing back", fake.Count(), asked.Load())
	}
	var b []byte
	waitUntil(t, "the check to be recorded", func() bool {
		b, _ = os.ReadFile(filepath.Join(r.h.home, "checker", "checks.jsonl"))
		return len(b) > 0
	})
	var rec check.Record
	if err := json.Unmarshal(b, &rec); err != nil || rec.SentBack || len(rec.Flags) != 1 || rec.Model != "eklow/bolt" ||
		rec.Chat != "signal:group:"+sigGroup+":"+sigAccount || !strings.Contains(rec.State, "I've logged it.") {
		t.Fatalf("record %s: %+v, %v", b, rec, err)
	}
}
