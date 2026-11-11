package server_test

// A chat's conversation starts over each day ([gateway] daily_reset, at
// daily_reset_hour): the first message after the reset hour went by with the
// session quiet opens a new session, which keeps the chat's /model pin.

import (
	"context"
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// backdate moves a session's messages and creation back by d, as if the chat
// had last been used then.
func backdate(t *testing.T, home, id string, d time.Duration) {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+filepath.Join(home, "soa-d.db")+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ts := time.Now().Add(-d).Format(time.RFC3339Nano)
	for _, q := range []string{`UPDATE messages SET ts = ? WHERE session_id = ?`, `UPDATE sessions SET created = ? WHERE id = ?`} {
		if _, err := db.Exec(q, ts, id); err != nil {
			t.Fatal(err)
		}
	}
}

func (r *chatRig) chatSession() string {
	r.t.Helper()
	id, err := openStore(r.t, r.h.home).ChatSession(context.Background(), "signal:group:"+sigGroup+":"+sigAccount)
	if err != nil {
		r.t.Fatal(err)
	}
	return id
}

func TestAChatStartsOverOnceTheResetHourPassesQuietly(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"}, fakellm.Turn{Content: "three"},
	)
	r := newChatRig(t, fake, "[gateway]\nmodel = \"other-model\"\n")
	r.say("first")
	r.reply(sigGroup, "one")
	r.say("/model test-model")
	r.reply(sigGroup, "Model: test-model, from the next message.")
	r.say("second")
	r.reply(sigGroup, "two")
	day1 := r.chatSession()
	if got := wireRoles(t, fake, 1); strings.Count(got, "user") != 2 {
		t.Fatalf("within the day the conversation carries on: %s", got)
	}

	backdate(t, r.h.home, day1, 25*time.Hour)
	r.say("third")
	r.reply(sigGroup, "three")
	day2 := r.chatSession()
	if day2 == day1 {
		t.Fatal("a session quiet over the reset hour is still the chat's")
	}
	if ms := wire(t, fake, 2); wireRoles(t, fake, 2) != "system,user" || !strings.Contains(str(ms[1], "content"), "third") {
		t.Fatalf("the new day's first request = %s; the new session holds only its own message", wireRoles(t, fake, 2))
	}
	if got := fake.Request(2)["model"]; got != "test-model" {
		t.Fatalf("the new session runs on %v; /model pins the chat, not the day", got)
	}
	if rows, _ := openStore(t, r.h.home).Messages(context.Background(), day1); len(rows) != 4 {
		t.Fatalf("yesterday's session holds %d messages; it is kept", len(rows))
	}
}

func TestDailyResetOffKeepsTheConversation(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := newChatRig(t, fake, "[gateway]\ndaily_reset = false\n")
	r.say("first")
	r.reply(sigGroup, "one")
	day1 := r.chatSession()
	backdate(t, r.h.home, day1, 25*time.Hour)
	r.say("second")
	r.reply(sigGroup, "two")
	if r.chatSession() != day1 || strings.Count(wireRoles(t, fake, 1), "user") != 2 {
		t.Fatal("with daily_reset = false a chat keeps its session")
	}
}

func TestARunningTurnIsNeverReset(t *testing.T) {
	release := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "long answer", Release: release},
		fakellm.Turn{Content: "queued answer"},
	)
	r := newChatRig(t, fake, "")
	r.say("first")
	waitUntil(t, "the first request", func() bool { return fake.Count() == 1 })
	day1 := r.chatSession()
	backdate(t, r.h.home, day1, 25*time.Hour)
	st := openStore(t, r.h.home)
	r.say("second")
	waitUntil(t, "the second message queued", func() bool {
		q, _ := st.QueueItems(context.Background(), day1)
		return len(q) == 1
	})
	close(release)
	r.reply(sigGroup, "queued answer")
	if r.chatSession() != day1 {
		t.Fatal("a message that arrives while a turn runs joins that turn's session")
	}
}

func wireRoles(t *testing.T, fake *fakellm.Server, i int) string {
	t.Helper()
	var rs []string
	for _, m := range wire(t, fake, i) {
		rs = append(rs, str(m, "role"))
	}
	return strings.Join(rs, ",")
}
