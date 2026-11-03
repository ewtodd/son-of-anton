package server_test

// Phase 5a through the daemon: Signal chats (daemon/phases/05a-signal.md),
// against internal/signalfake and internal/fakellm. The owner writes in a
// group from their phone, which signal-cli (a linked device of the same
// account) sees as a sync message.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const (
	sigAccount = "+15550000000"
	sigMember  = "+15551111111"
	sigGroup   = "gW"
	sigGroup2  = "gX"
	queuedAck  = "Queued for the next turn. I'll respond once the current task finishes."
)

type chatRig struct {
	t    *testing.T
	h    *harness
	sig  *signalfake.Server
	fake *fakellm.Server
	ts   int64
	lock string // the lock directory
}

// setSignalEnv points the daemon at sig, as the Nix module sets it up.
func setSignalEnv(t *testing.T, sig *signalfake.Server) string {
	t.Setenv("SIGNAL_HTTP_URL", sig.URL)
	t.Setenv("SIGNAL_ACCOUNT", sigAccount)
	t.Setenv("SIGNAL_ALLOWED_USERS", sigAccount+","+sigMember)
	t.Setenv("SIGNAL_GROUP_ALLOWED_USERS", sigGroup+","+sigGroup2)
	t.Setenv("SIGNAL_DM_MODE", "ignore")
	t.Setenv("SIGNAL_REQUIRE_MENTION", "")
	lock := t.TempDir()
	t.Setenv("SON_OF_ANTON_GATEWAY_LOCK_DIR", lock)
	return lock
}

func newChatRig(t *testing.T, fake *fakellm.Server, extra string) *chatRig {
	t.Helper()
	sig := signalfake.New(t)
	lock := setSignalEnv(t, sig)
	h := newHarness(t, fake, "[daemon]\nsignal = true\n\n"+extra)
	sig.WaitConnected(t)
	return &chatRig{t: t, h: h, sig: sig, fake: fake, ts: 1_700_000_000_000, lock: lock}
}

// say is the owner writing text in group g (sigGroup when "").
func (r *chatRig) say(text string) int64 { return r.sayIn(sigGroup, text) }

func (r *chatRig) sayIn(g, text string) int64 {
	r.ts++
	r.sig.Push(signalfake.OwnerInGroup(sigAccount, g, text, r.ts))
	return r.ts
}

// reply waits for a message sent to group g with exactly text.
func (r *chatRig) reply(g, text string) signalfake.Sent {
	r.t.Helper()
	return r.sig.WaitSend(r.t, "of "+strconv.Quote(text), func(m signalfake.Sent) bool { return m.To == "group:"+g && m.Text == text })
}

// replyLike waits for a message sent to group g containing every part.
func (r *chatRig) replyLike(g string, parts ...string) signalfake.Sent {
	r.t.Helper()
	return r.sig.WaitSend(r.t, "containing "+strings.Join(parts, " + "), func(m signalfake.Sent) bool {
		if m.To != "group:"+g {
			return false
		}
		for _, p := range parts {
			if !strings.Contains(m.Text, p) {
				return false
			}
		}
		return true
	})
}

func (r *chatRig) requests(n int) {
	r.t.Helper()
	waitUntil(r.t, strconv.Itoa(n)+" model requests", func() bool { return r.fake.Count() >= n })
}

// quiet waits a moment and checks that nothing more reached the model.
func (r *chatRig) quiet(requests int) {
	r.t.Helper()
	time.Sleep(400 * time.Millisecond)
	if n := r.fake.Count(); n != requests {
		r.t.Fatalf("model requests = %d, want %d", n, requests)
	}
}

// lastUser is the last user message of model request i.
func lastUser(t *testing.T, fake *fakellm.Server, i int) string {
	t.Helper()
	ms := wire(t, fake, i)
	for j := len(ms) - 1; j >= 0; j-- {
		if str(ms[j], "role") == "user" {
			return str(ms[j], "content")
		}
	}
	return ""
}

func openStore(t *testing.T, home string) *store.Store {
	t.Helper()
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func TestSignalIsOffByDefault(t *testing.T) {
	fake := fakellm.New(t)
	sig := signalfake.New(t)
	setSignalEnv(t, sig)
	newHarness(t, fake, "")
	time.Sleep(500 * time.Millisecond)
	if n := sig.Connects(); n != 0 {
		t.Fatalf("connects = %d; Signal runs only with [daemon] signal = true", n)
	}
}

func TestAChatMessageGetsAnAnswer(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "**Hi** there"})
	r := newChatRig(t, fake, "")
	r.say("hello bot")
	m := r.reply(sigGroup, "Hi there")
	if strings.Join(m.Styles, ",") != "0:2:BOLD" {
		t.Fatalf("styles = %v; replies go through Format", m.Styles)
	}
	if got := lastUser(t, fake, 0); got != "hello bot" {
		t.Fatalf("the model got %q", got)
	}
	names := toolNames(t, fake.Request(0))
	sort.Strings(names)
	if strings.Join(names, ",") != "clarify,memory,patch,read_file,relay,search_files,session_search,todo,web_extract,web_search,write_file" {
		t.Fatalf("a chat's tools = %v", names)
	}
	var start, stop bool
	for _, p := range r.sig.Calls("sendTyping") {
		if p["groupId"] == sigGroup {
			if p["stop"] == true {
				stop = true
			} else {
				start = true
			}
		}
	}
	if !start || !stop {
		t.Fatalf("typing = %v", r.sig.Calls("sendTyping"))
	}

	// signal-cli echoes our reply back as a sync message from the account.
	r.sig.Push(signalfake.OwnerInGroup(sigAccount, sigGroup, "Hi there", m.TS))
	r.quiet(1)
}

func TestABusyChatQueuesAndAcksOnce(t *testing.T) {
	hold := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer", Release: hold},
		fakellm.Turn{Content: "second answer"},
		fakellm.Turn{Content: "third answer"},
	)
	r := newChatRig(t, fake, "")
	r.say("one")
	r.requests(1)
	r.say("two")
	r.reply(sigGroup, queuedAck)
	r.say("three")
	time.Sleep(300 * time.Millisecond)
	acks := 0
	for _, m := range r.sig.Sends() {
		if m.Text == queuedAck {
			acks++
		}
	}
	if acks != 1 {
		t.Fatalf("acks = %d; at most one per 30 s", acks)
	}
	close(hold)
	r.reply(sigGroup, "first answer")
	r.reply(sigGroup, "second answer")
	r.reply(sigGroup, "third answer")
	if lastUser(t, fake, 1) != "two" || lastUser(t, fake, 2) != "three" {
		t.Fatalf("each queued message is its own turn: %q, %q", lastUser(t, fake, 1), lastUser(t, fake, 2))
	}
}

func TestStopAndNew(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Hang: true},
		fakellm.Turn{Content: "noted"},
		fakellm.Turn{Content: "no idea"},
	)
	r := newChatRig(t, fake, "")
	r.say("a long job")
	r.requests(1)
	r.say("/stop")
	r.reply(sigGroup, "Stopped. You can continue this session.")
	r.say("/stop")
	r.reply(sigGroup, "No active task to stop.")

	r.say("the word is kiwi")
	r.reply(sigGroup, "noted")
	r.say("/new")
	r.reply(sigGroup, "New session started.")
	r.say("what was the word?")
	r.reply(sigGroup, "no idea")
	for _, m := range wire(t, fake, 2) {
		if strings.Contains(str(m, "content"), "kiwi") {
			t.Fatal("a new session starts empty")
		}
	}
	st := openStore(t, r.h.home)
	chats, err := st.ListSessions(context.Background(), []string{"chat"}, 10)
	if err != nil || len(chats) != 2 {
		t.Fatalf("chat sessions = %d, %v; /new keeps the old one", len(chats), err)
	}
	for _, c := range chats {
		if c.Session.Scope != "gateway" || c.Session.Chat != "signal:group:"+sigGroup+":"+sigAccount || c.Session.Source != "signal" {
			t.Fatalf("a chat session = %+v", c.Session)
		}
	}
}

func TestCommandsNeverReachTheModel(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "checking"})
	r := newChatRig(t, fake, "")
	r.say("/frobnicate")
	r.reply(sigGroup, "Unknown command /frobnicate. Commands: /new, /stop, /status, /sendnow, /sendall, /approve, /deny.")
	r.say("/sendnow")
	r.reply(sigGroup, "Nothing is running.")
	r.say("/sendall")
	r.reply(sigGroup, "Nothing is running.")
	r.say("/approve")
	r.reply(sigGroup, "Nothing is waiting for approval.")
	r.say("/status")
	r.replyLike(sigGroup, "test-model", "idle")
	r.quiet(0)
	r.say("/etc/hosts looks wrong")
	r.reply(sigGroup, "checking")
	if lastUser(t, fake, 0) != "/etc/hosts looks wrong" {
		t.Fatal("a path is a message, not a command")
	}
}

func TestSendnowSteersTheRunningTurn(t *testing.T) {
	hold := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "going right", Release: hold},
		fakellm.Turn{Content: "went left"},
	)
	r := newChatRig(t, fake, "")
	r.say("start walking")
	r.requests(1)
	r.say("/sendnow go left instead")
	r.reply(sigGroup, "Steered into the current turn.")
	close(hold)
	r.reply(sigGroup, "went left")
	if !strings.Contains(lastUser(t, fake, 1), "go left instead") {
		t.Fatalf("the steer turn = %q", lastUser(t, fake, 1))
	}
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "going right") {
			t.Fatal("a steered turn sends nothing")
		}
	}
}

func TestAChatApprovesARule(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("m1", "memory", `{"kind":"rule","action":"add","text":"Answer in plain English"}`)}},
		fakellm.Turn{Content: "saved it"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("m2", "memory", `{"kind":"rule","action":"add","text":"Never use tables"}`)}},
		fakellm.Turn{Content: "left it"},
	)
	r := newChatRig(t, fake, "")
	rules := func() string {
		b, _ := os.ReadFile(filepath.Join(r.h.home, "memories", "rules.toml"))
		return string(b)
	}
	r.say("remember: plain English")
	r.replyLike(sigGroup, "⚠️ Approval needed", "Answer in plain English", "Reply /approve")
	r.say("/approve")
	r.reply(sigGroup, "Approved.")
	r.reply(sigGroup, "saved it")
	if !strings.Contains(rules(), "Answer in plain English") {
		t.Fatal("the approved rule is saved")
	}
	r.say("and never use tables")
	r.replyLike(sigGroup, "⚠️ Approval needed", "Never use tables")
	r.say("no")
	r.reply(sigGroup, "Denied.")
	r.reply(sigGroup, "left it")
	if strings.Contains(rules(), "Never use tables") {
		t.Fatal("a plain no denies")
	}
}

func TestAChatAnswersAQuestion(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("q1", "clarify", `{"question":"Which flavour?","choices":["vanilla","chocolate"]}`)}},
		fakellm.Turn{Content: "picked"},
	)
	r := newChatRig(t, fake, "")
	r.say("ask me")
	r.reply(sigGroup, "❓ Which flavour?\n\n  1. vanilla\n  2. chocolate\n\nReply with the number, the option text, or your own answer.")
	r.say("2")
	r.reply(sigGroup, "picked")
	var result string
	for _, m := range wire(t, fake, 1) {
		if str(m, "role") == "tool" {
			result = str(m, "content")
		}
	}
	if !strings.Contains(result, "chocolate") {
		t.Fatalf("the answer = %s", result)
	}
}

func TestStrangersGetNothing(t *testing.T) {
	fake := fakellm.New(t)
	r := newChatRig(t, fake, "")
	r.sig.SetAttachment("a1", []byte("PNG"), false)
	stranger := signalfake.GroupMessage("+15559999999", sigGroup, "hi", 1)
	stranger["envelope"].(map[string]any)["dataMessage"].(map[string]any)["attachments"] = []any{
		map[string]any{"id": "a1", "contentType": "image/png", "filename": "x.png", "size": 3}}
	r.sig.Push(stranger)
	r.sig.Push(signalfake.GroupMessage(sigMember, "gOther", "wrong group", 2))
	r.quiet(0)
	if n := len(r.sig.Calls("send")) + len(r.sig.Calls("getAttachment")) + len(r.sig.Calls("sendTyping")); n != 0 {
		t.Fatalf("calls for a stranger: %v", r.sig.Calls(""))
	}
}

func TestAttachmentsAndQuotes(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "nice picture"}, fakellm.Turn{Content: "a cat"})
	r := newChatRig(t, fake, "")
	r.sig.SetAttachment("a1", []byte("\x89PNG fake"), false)
	r.ts++
	env := signalfake.OwnerInGroup(sigAccount, sigGroup, "look", r.ts)
	sent := env["envelope"].(map[string]any)["syncMessage"].(map[string]any)["sentMessage"].(map[string]any)
	sent["attachments"] = []any{map[string]any{"id": "a1", "contentType": "image/png", "filename": "shot.png", "size": 9}}
	r.sig.Push(env)
	m := r.reply(sigGroup, "nice picture")
	got := lastUser(t, fake, 0)
	found := regexp.MustCompile(`^look\n\[The user sent shot\.png \(image/png\), saved at (.+)\]$`).FindStringSubmatch(got)
	if found == nil {
		t.Fatalf("the user text = %q", got)
	}
	if b, err := os.ReadFile(found[1]); err != nil || string(b) != "\x89PNG fake" || !strings.HasPrefix(found[1], filepath.Join(r.h.home, "cache", "signal")+string(filepath.Separator)) {
		t.Fatalf("saved at %s: %q, %v", found[1], b, err)
	}

	r.ts++
	env = signalfake.OwnerInGroup(sigAccount, sigGroup, "what is in it?", r.ts)
	sent = env["envelope"].(map[string]any)["syncMessage"].(map[string]any)["sentMessage"].(map[string]any)
	sent["quote"] = map[string]any{"id": m.TS, "text": "nice picture", "author": sigAccount}
	r.sig.Push(env)
	r.reply(sigGroup, "a cat")
	if got := lastUser(t, fake, 1); got != "[Replying to your previous message: \"nice picture\"]\n\nwhat is in it?" {
		t.Fatalf("a quote of our reply = %q", got)
	}
}

func TestTheWallKeepsChatsOffTheSocket(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "the secret is papaya"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("s1", "session_search", `{"query":"papaya"}`)}},
		fakellm.Turn{Content: "nothing found"},
	)
	r := newChatRig(t, fake, "")
	r.say("tell me the secret")
	r.reply(sigGroup, "the secret is papaya")

	st := openStore(t, r.h.home)
	chats, _ := st.ListSessions(context.Background(), []string{"chat"}, 10)
	if len(chats) != 1 {
		t.Fatalf("chat sessions = %d", len(chats))
	}
	id := chats[0].Session.ID
	c := r.h.dial()
	for _, kinds := range [][]string{nil, {"chat"}, {"interactive", "chat"}} {
		for _, s := range objs(c.ok("session.list", map[string]any{"kinds": kinds}), "sessions") {
			if str(s, "kind") == "chat" || str(s, "id") == id {
				t.Fatalf("session.list (%v) shows a chat: %v", kinds, s)
			}
		}
	}
	c.fails("session.attach", map[string]any{"session": id, "as": "viewer"}, -32011)
	c.fails("session.history", map[string]any{"session": id}, -32011)
	c.fails("session.children", map[string]any{"session": id}, -32011)

	tui := c.create(t.TempDir())
	c.attach(tui, "driver")
	c.send(tui, "what was the secret?")
	rows := toolRows(c.turn())
	// The result repeats the query, so look for the chat's own text.
	if len(rows) != 1 || strings.Contains(rows[0], "the secret is papaya") || strings.Contains(rows[0], id) {
		t.Fatalf("a terminal session's search reached a chat: %v", rows)
	}
}

func chatSearchRig(t *testing.T, extra string) (*chatRig, string) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "noted mango"},
		fakellm.Turn{Content: "noted lychee"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("s1", "session_search", `{"query":"mango"}`)}},
		fakellm.Turn{Content: "searched mango"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("s2", "session_search", `{"query":"lychee"}`)}},
		fakellm.Turn{Content: "searched lychee"},
	)
	r := newChatRig(t, fake, extra)
	r.sayIn(sigGroup, "the code word is mango")
	r.reply(sigGroup, "noted mango")
	c := r.h.dial()
	tui := c.create(t.TempDir())
	c.attach(tui, "driver")
	c.send(tui, "the common word is lychee")
	c.turn()
	r.sayIn(sigGroup2, "find the code word")
	r.reply(sigGroup2, "searched mango")
	r.sayIn(sigGroup2, "find the common word")
	r.reply(sigGroup2, "searched lychee")
	var mango, lychee string
	for i := 0; i < fake.Count(); i++ {
		for _, m := range wire(t, fake, i) {
			if str(m, "role") == "tool" && strings.Contains(str(m, "content"), `"mode":"discover"`) {
				if strings.Contains(str(m, "content"), `"query":"mango"`) {
					mango = str(m, "content")
				} else {
					lychee = str(m, "content")
				}
			}
		}
	}
	if !strings.Contains(lychee, "lychee") || !strings.Contains(lychee, tui) {
		t.Fatalf("a chat reads common sessions: %s", lychee)
	}
	return r, mango
}

func TestChatsAreWalledFromEachOther(t *testing.T) {
	_, mango := chatSearchRig(t, "")
	var res struct{ Count int }
	if err := json.Unmarshal([]byte(mango), &res); err != nil || res.Count != 0 || strings.Contains(mango, "the code word is mango") {
		t.Fatalf("on a shared instance another chat's conversation is walled: %s", mango)
	}
}

func TestOneUsersChatsShareEverything(t *testing.T) {
	_, mango := chatSearchRig(t, "[gateway]\nsingle_user = true\n")
	if !strings.Contains(mango, "the code word is mango") {
		t.Fatalf("with single_user a chat reads the other chats: %s", mango)
	}
}

// ---- the lock ----

func lockPath(dir string) string {
	sum := sha256.Sum256([]byte(sigAccount))
	return filepath.Join(dir, "signal-phone-"+hex.EncodeToString(sum[:])[:16]+".lock")
}

func startTime(t *testing.T, pid int) int64 {
	t.Helper()
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	fields := strings.Fields(s[strings.LastIndexByte(s, ')')+2:])
	v, err := strconv.ParseInt(fields[19], 10, 64) // field 22 overall
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func writeLock(t *testing.T, dir string, pid int, start int64) {
	t.Helper()
	rec := map[string]any{"pid": pid, "start_time": start, "scope": "signal-phone", "identity_hash": "x",
		"metadata": map[string]any{"platform": "signal"}, "updated_at": "2026-11-02T00:00:00+00:00"}
	b, _ := json.Marshal(rec)
	if err := os.WriteFile(lockPath(dir), b, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestALiveGatewayHoldsTheAccount(t *testing.T) {
	holder := exec.Command("sleep", "30")
	if err := holder.Start(); err != nil {
		t.Skip("no sleep binary:", err)
	}
	t.Cleanup(func() { _ = holder.Process.Kill(); _ = holder.Wait() })
	fake := fakellm.New(t)
	sig := signalfake.New(t)
	lock := setSignalEnv(t, sig)
	writeLock(t, lock, holder.Process.Pid, startTime(t, holder.Process.Pid))
	newHarness(t, fake, "[daemon]\nsignal = true\n")
	time.Sleep(500 * time.Millisecond)
	if sig.Connects() != 0 {
		t.Fatal("another live gateway holds the account: Signal stays off")
	}
	b, _ := os.ReadFile(lockPath(lock))
	if !strings.Contains(string(b), `"pid":`+strconv.Itoa(holder.Process.Pid)) {
		t.Fatalf("the holder's lock is untouched: %s", b)
	}
}

func TestAStaleLockIsTaken(t *testing.T) {
	gone := exec.Command("true")
	if err := gone.Run(); err != nil {
		t.Skip("no true binary:", err)
	}
	fake := fakellm.New(t)
	sig := signalfake.New(t)
	lock := setSignalEnv(t, sig)
	writeLock(t, lock, gone.Process.Pid, 1)
	h := newHarness(t, fake, "[daemon]\nsignal = true\n")
	sig.WaitConnected(t)
	var rec map[string]any
	b, _ := os.ReadFile(lockPath(lock))
	if err := json.Unmarshal(b, &rec); err != nil || int(rec["pid"].(float64)) != os.Getpid() || rec["scope"] != "signal-phone" || rec["start_time"] == nil {
		t.Fatalf("the lock now = %s, %v", b, err)
	}
	if err := h.stop(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(lockPath(lock)); !os.IsNotExist(err) {
		t.Fatalf("the lock is removed at shutdown: %v", err)
	}
}
