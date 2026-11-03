package signal_test

// Phase 5a: what the intake accepts and drops (daemon/phases/05a-signal.md,
// "Intake"), against envelopes shaped as signal-cli sends them.

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

const (
	account = "+15550000000"
	member  = "+15551111111"
	group   = "gW"
)

func cfg() signal.Config {
	return signal.Config{Account: account, Senders: []string{account, member}, Groups: []string{group}, DMMode: "allow", PerUser: true}
}

func raw(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func accept(t *testing.T, in *signal.Intake, env any) (signal.Inbound, bool) {
	t.Helper()
	return in.Accept(raw(t, env))
}

func TestConfigFromEnv(t *testing.T) {
	env := map[string]string{
		"SIGNAL_HTTP_URL": "http://127.0.0.1:8080/", "SIGNAL_ACCOUNT": account,
		"SIGNAL_ALLOWED_USERS": account + ", " + member, "SIGNAL_GROUP_ALLOWED_USERS": "gA,gB",
		"SIGNAL_DM_MODE": "ignore", "SIGNAL_REQUIRE_MENTION": "yes",
	}
	c := signal.ConfigFromEnv(func(k string) string { return env[k] })
	if c.URL != "http://127.0.0.1:8080" || c.Account != account || strings.Join(c.Senders, ",") != account+","+member ||
		strings.Join(c.Groups, ",") != "gA,gB" || c.DMMode != "ignore" || !c.RequireMention || !c.PerUser {
		t.Fatalf("ConfigFromEnv = %+v", c)
	}
	env["SIGNAL_DM_MODE"] = "bogus"
	if c := signal.ConfigFromEnv(func(k string) string { return env[k] }); c.DMMode != "allow" {
		t.Fatalf("an unknown DM mode counts as allow: %q", c.DMMode)
	}
}

func TestAGroupMessageIsAccepted(t *testing.T) {
	in := signal.NewIntake(cfg())
	m, ok := accept(t, in, signalfake.GroupMessage(member, group, "hello there", 100))
	if !ok || m.Chat != "signal:group:gW:"+member || m.To != "group:gW" || m.Text != "hello there" ||
		m.Sender != member || m.Group != group || m.GroupName != "The Group" || m.Timestamp != 100 {
		t.Fatalf("Accept = %+v, %v", m, ok)
	}
	c := cfg()
	c.PerUser = false
	if m, _ := accept(t, signal.NewIntake(c), signalfake.GroupMessage(member, group, "hi", 101)); m.Chat != "signal:group:gW" {
		t.Fatalf("one session per group: %q", m.Chat)
	}
}

func TestTheOwnerWritingFromTheirPhone(t *testing.T) {
	in := signal.NewIntake(cfg())
	m, ok := accept(t, in, signalfake.OwnerInGroup(account, group, "from my phone", 200))
	if !ok || m.Chat != "signal:group:gW:"+account || m.To != "group:gW" || m.Text != "from my phone" || m.Sender != account {
		t.Fatalf("the owner in a group = %+v, %v", m, ok)
	}
	m, ok = accept(t, in, signalfake.NoteToSelf(account, "a note", 201))
	if !ok || m.Chat != "signal:dm:"+account || m.To != account || m.Text != "a note" {
		t.Fatalf("Note to Self = %+v, %v", m, ok)
	}
}

func TestOurOwnRepliesAreNotAnswered(t *testing.T) {
	in := signal.NewIntake(cfg())
	in.Sent(300)
	if _, ok := accept(t, in, signalfake.OwnerInGroup(account, group, "the bot's reply", 300)); ok {
		t.Fatal("the echo of our own send is dropped")
	}
	if _, ok := accept(t, in, signalfake.OwnerInGroup(account, group, "a real message", 301)); !ok {
		t.Fatal("only our own timestamps are echoes")
	}
	own := signalfake.DM(account, "from the account, not a sync", 302)
	if _, ok := accept(t, in, own); ok {
		t.Fatal("a data message from the account itself is dropped")
	}
	readSync := map[string]any{"envelope": map[string]any{"source": account, "sourceNumber": account, "timestamp": 303,
		"syncMessage": map[string]any{"readMessages": []any{map[string]any{"sender": member, "timestamp": 1}}}}}
	if _, ok := accept(t, in, readSync); ok {
		t.Fatal("other sync messages are dropped")
	}
}

func TestWhoMayTalk(t *testing.T) {
	in := signal.NewIntake(cfg())
	if _, ok := accept(t, in, signalfake.GroupMessage("+15559999999", group, "hi", 1)); ok {
		t.Fatal("a sender not allowed")
	}
	if _, ok := accept(t, in, signalfake.GroupMessage(member, "gOther", "hi", 2)); ok {
		t.Fatal("a group not allowed")
	}
	c := cfg()
	c.Groups = nil
	if _, ok := accept(t, signal.NewIntake(c), signalfake.GroupMessage(member, group, "hi", 3)); ok {
		t.Fatal("no group list drops every group")
	}
	c.Groups = []string{"*"}
	if _, ok := accept(t, signal.NewIntake(c), signalfake.GroupMessage(member, "gAny", "hi", 4)); !ok {
		t.Fatal("* allows any group")
	}
	c = cfg()
	c.Senders = nil
	if _, ok := accept(t, signal.NewIntake(c), signalfake.GroupMessage(member, group, "hi", 5)); ok {
		t.Fatal("no sender list denies everyone")
	}
	c.Senders = []string{"*"}
	if _, ok := accept(t, signal.NewIntake(c), signalfake.GroupMessage("+15559999999", group, "hi", 6)); !ok {
		t.Fatal("* allows anyone")
	}
	byUUID := signalfake.GroupMessage("", group, "hi", 7)
	e := byUUID["envelope"].(map[string]any)
	delete(e, "source")
	delete(e, "sourceNumber")
	e["sourceUuid"] = "uuid-member"
	c = cfg()
	c.Senders = []string{"uuid-member"}
	if m, ok := accept(t, signal.NewIntake(c), byUUID); !ok || m.Sender != "uuid-member" {
		t.Fatalf("a sender allowed by UUID: %+v, %v", m, ok)
	}
}

func TestDirectMessages(t *testing.T) {
	m, ok := accept(t, signal.NewIntake(cfg()), signalfake.DM(member, "psst", 1))
	if !ok || m.Chat != "signal:dm:"+member || m.To != member {
		t.Fatalf("a DM = %+v, %v", m, ok)
	}
	c := cfg()
	c.DMMode = "ignore"
	in := signal.NewIntake(c)
	if _, ok := accept(t, in, signalfake.DM(member, "psst", 2)); ok {
		t.Fatal("ignore drops DMs")
	}
	if _, ok := accept(t, in, signalfake.NoteToSelf(account, "still mine", 3)); !ok {
		t.Fatal("Note to Self survives ignore")
	}
}

func TestWhatIsNotAMessage(t *testing.T) {
	in := signal.NewIntake(cfg())
	empty := signalfake.GroupMessage(member, group, "   ", 1)
	if _, ok := accept(t, in, empty); ok {
		t.Fatal("no text and no attachments")
	}
	typing := map[string]any{"envelope": map[string]any{"source": member, "sourceNumber": member, "timestamp": 2,
		"typingMessage": map[string]any{"action": "STARTED"}}}
	if _, ok := accept(t, in, typing); ok {
		t.Fatal("typing")
	}
	receipt := map[string]any{"envelope": map[string]any{"source": member, "sourceNumber": member, "timestamp": 3,
		"receiptMessage": map[string]any{"isRead": true, "timestamps": []any{1}}}}
	if _, ok := accept(t, in, receipt); ok {
		t.Fatal("a receipt")
	}
	story := map[string]any{"envelope": map[string]any{"source": member, "sourceNumber": member, "timestamp": 4,
		"storyMessage": map[string]any{"textAttachment": map[string]any{"text": "story"}}}}
	if _, ok := accept(t, in, story); ok {
		t.Fatal("a story")
	}
	edit := map[string]any{"envelope": map[string]any{"source": member, "sourceNumber": member, "timestamp": 5,
		"editMessage": map[string]any{"targetSentTimestamp": 1, "dataMessage": map[string]any{
			"timestamp": 5, "message": "fixed typo", "groupInfo": map[string]any{"groupId": group}}}}}
	if m, ok := accept(t, in, edit); !ok || m.Text != "fixed typo" {
		t.Fatalf("an edit is a new message: %+v, %v", m, ok)
	}
}

func TestMentionsUseUTF16Offsets(t *testing.T) {
	env := signalfake.GroupMessage(member, group, "👍 ￼ look", 1)
	dm := env["envelope"].(map[string]any)["dataMessage"].(map[string]any)
	dm["mentions"] = []any{map[string]any{"start": 3, "length": 1, "number": "+15552222222", "uuid": "u2"}}
	m, ok := accept(t, signal.NewIntake(cfg()), env)
	if !ok || m.Text != "👍 @+15552222222 look" {
		t.Fatalf("mention = %q", m.Text)
	}
}

func TestRequireMention(t *testing.T) {
	c := cfg()
	c.RequireMention = true
	in := signal.NewIntake(c)
	if _, ok := accept(t, in, signalfake.GroupMessage(member, group, "chatter", 1)); ok {
		t.Fatal("no mention, no message")
	}
	env := signalfake.GroupMessage(member, group, "￼ what now?", 2)
	dm := env["envelope"].(map[string]any)["dataMessage"].(map[string]any)
	dm["mentions"] = []any{map[string]any{"start": 0, "length": 1, "number": account}}
	if m, ok := accept(t, in, env); !ok || m.Text != "what now?" {
		t.Fatalf("a mention of the account, stripped: %q, %v", m.Text, ok)
	}
	if m, ok := accept(t, in, signalfake.GroupMessage(member, group, "hey @"+account+"  there", 3)); !ok || m.Text != "hey there" {
		t.Fatalf("a typed @account: %q, %v", m.Text, ok)
	}
	if _, ok := accept(t, in, signalfake.DM(member, "no mention needed", 4)); !ok {
		t.Fatal("DMs need no mention")
	}
}

func TestQuotesAndAttachments(t *testing.T) {
	in := signal.NewIntake(cfg())
	in.Sent(500)
	env := signalfake.GroupMessage(member, group, "and this?", 600)
	dm := env["envelope"].(map[string]any)["dataMessage"].(map[string]any)
	dm["quote"] = map[string]any{"id": 500, "text": "the earlier answer", "author": account}
	dm["attachments"] = []any{
		map[string]any{"id": "att1", "contentType": "image/png", "filename": "shot.png", "size": 1234},
		map[string]any{"id": "att2", "contentType": "video/mp4", "size": 200 << 20},
	}
	m, ok := accept(t, in, env)
	if !ok || m.Quote == nil || m.Quote.ID != 500 || m.Quote.Text != "the earlier answer" || !m.Quote.Own {
		t.Fatalf("quote = %+v", m.Quote)
	}
	if len(m.Attachments) != 1 || m.Attachments[0] != (signal.Attachment{ID: "att1", ContentType: "image/png", Filename: "shot.png", Size: 1234}) {
		t.Fatalf("attachments = %+v; over 100 MB is skipped", m.Attachments)
	}
	env = signalfake.GroupMessage(member, group, "", 601)
	dm = env["envelope"].(map[string]any)["dataMessage"].(map[string]any)
	dm["quote"] = map[string]any{"id": 42, "text": "someone else", "author": account}
	dm["attachments"] = []any{map[string]any{"id": "att3", "contentType": "application/pdf", "size": 10}}
	m, ok = accept(t, in, env)
	if !ok || m.Quote.Own || len(m.Attachments) != 1 {
		t.Fatalf("an attachment alone is a message; a quote of not ours is not Own: %+v, %v", m, ok)
	}
}
