package signal

import (
	"encoding/json"
	"strings"
	"sync"
	"time"
	"unicode/utf16"
)

const (
	maxAttachment = 100 * 1024 * 1024
	echoTTL       = 5 * time.Minute
	echoKeep      = 512
	ownKeep       = 500
	objectChar    = "￼" // the placeholder a mention leaves in the text
)

// Attachment is a file on a message.
type Attachment struct {
	ID, ContentType, Filename string
	Size                      int64
}

// Quote is the message a reply answers.
type Quote struct {
	ID   int64
	Text string
	Own  bool // it quotes one of our replies
}

// Inbound is a message for the agent.
type Inbound struct {
	Chat                                             string // the chat stamp, and the session key
	To                                               string // where replies go: the sender, or "group:<id>"
	Sender, SenderUUID, SenderName, Group, GroupName string
	Text                                             string
	Timestamp                                        int64
	Attachments                                      []Attachment
	Quote                                            *Quote
}

type sentRecord struct {
	ts     int64
	at     time.Time
	echoed bool
}

// Intake decides which of signal-cli's events are messages for the agent
// (gateway/platforms/signal.py _handle_envelope), and remembers what we sent
// so the echoes of our own replies are not answered.
type Intake struct {
	cfg Config

	mu      sync.Mutex
	ownUUID string
	sent    []sentRecord
}

// NewIntake returns an intake for cfg.
func NewIntake(cfg Config) *Intake { return &Intake{cfg: cfg} }

// Sent records the timestamp of a message we sent. signal-cli's linked device
// sees our replies come back as the account's own sync messages.
func (in *Intake) Sent(ts int64) {
	if ts == 0 {
		return
	}
	in.mu.Lock()
	defer in.mu.Unlock()
	in.sent = append(in.sent, sentRecord{ts: ts, at: time.Now()})
	if len(in.sent) > echoKeep {
		in.sent = in.sent[len(in.sent)-echoKeep:]
	}
}

// consumeEcho reports whether ts is a recent send of ours that has not been
// seen coming back, and marks it seen.
func (in *Intake) consumeEcho(ts int64) bool {
	in.mu.Lock()
	defer in.mu.Unlock()
	for i := range in.sent {
		r := &in.sent[i]
		if r.ts == ts && !r.echoed && time.Since(r.at) <= echoTTL {
			r.echoed = true
			return true
		}
	}
	return false
}

// isOwn reports whether ts is one of our last sends.
func (in *Intake) isOwn(ts int64) bool {
	in.mu.Lock()
	defer in.mu.Unlock()
	from := max(0, len(in.sent)-ownKeep)
	for _, r := range in.sent[from:] {
		if r.ts == ts {
			return true
		}
	}
	return false
}

func (in *Intake) rememberOwnUUID(uuid string) {
	in.mu.Lock()
	in.ownUUID = uuid
	in.mu.Unlock()
}

func (in *Intake) own() string {
	in.mu.Lock()
	defer in.mu.Unlock()
	return in.ownUUID
}

type wireMention struct {
	Start  int    `json:"start"`
	Length int    `json:"length"`
	Number string `json:"number"`
	UUID   string `json:"uuid"`
}

type wireData struct {
	Timestamp int64  `json:"timestamp"`
	Message   string `json:"message"`
	GroupInfo *struct {
		GroupID   string `json:"groupId"`
		GroupName string `json:"groupName"`
	} `json:"groupInfo"`
	Mentions []wireMention `json:"mentions"`
	Quote    *struct {
		ID   int64  `json:"id"`
		Text string `json:"text"`
	} `json:"quote"`
	Attachments []struct {
		ID          string `json:"id"`
		ContentType string `json:"contentType"`
		Filename    string `json:"filename"`
		Size        int64  `json:"size"`
	} `json:"attachments"`
}

type wireSent struct {
	wireData
	Destination       string `json:"destination"`
	DestinationNumber string `json:"destinationNumber"`
}

type wireEnvelope struct {
	Source       string          `json:"source"`
	SourceNumber string          `json:"sourceNumber"`
	SourceUUID   string          `json:"sourceUuid"`
	SourceName   string          `json:"sourceName"`
	Timestamp    int64           `json:"timestamp"`
	DataMessage  *wireData       `json:"dataMessage"`
	StoryMessage json.RawMessage `json:"storyMessage"`
	EditMessage  *struct {
		DataMessage *wireData `json:"dataMessage"`
	} `json:"editMessage"`
	SyncMessage *struct {
		SentMessage *wireSent `json:"sentMessage"`
	} `json:"syncMessage"`
}

// Accept reads one event payload and returns the message in it, if any.
func (in *Intake) Accept(raw []byte) (Inbound, bool) {
	var top struct {
		Envelope *wireEnvelope `json:"envelope"`
	}
	if json.Unmarshal(raw, &top) != nil {
		return Inbound{}, false
	}
	env := top.Envelope
	if env == nil {
		env = new(wireEnvelope)
		if json.Unmarshal(raw, env) != nil {
			return Inbound{}, false
		}
	}
	account := in.cfg.Account

	sender := env.SourceNumber
	if sender == "" {
		sender = env.SourceUUID
	}
	if sender == "" {
		sender = env.Source
	}
	if sender == "" {
		return Inbound{}, false
	}
	if sender == account && env.SourceUUID != "" {
		in.rememberOwnUUID(env.SourceUUID)
	}

	data := env.DataMessage
	noteToSelf := false
	if env.SyncMessage != nil {
		sm := env.SyncMessage.SentMessage
		if sm == nil {
			return Inbound{}, false
		}
		dest := sm.DestinationNumber
		if dest == "" {
			dest = sm.Destination
		}
		inGroup := sm.GroupInfo != nil && sm.GroupInfo.GroupID != ""
		if !inGroup && dest != account {
			return Inbound{}, false
		}
		if in.consumeEcho(sm.Timestamp) {
			return Inbound{}, false
		}
		data, noteToSelf = &sm.wireData, !inGroup
	} else if sender == account {
		return Inbound{}, false
	}
	if len(env.StoryMessage) > 0 && string(env.StoryMessage) != "null" {
		return Inbound{}, false
	}
	if data == nil && env.EditMessage != nil {
		data = env.EditMessage.DataMessage
	}
	if data == nil {
		return Inbound{}, false
	}

	group, groupName := "", ""
	if data.GroupInfo != nil {
		group, groupName = data.GroupInfo.GroupID, data.GroupInfo.GroupName
	}
	if group != "" {
		if !listed(in.cfg.Groups, group) {
			return Inbound{}, false
		}
	} else if in.cfg.DMMode == "ignore" && !noteToSelf {
		return Inbound{}, false
	}

	if !listed(in.cfg.Senders, sender) && !listed(in.cfg.Senders, env.SourceUUID) {
		return Inbound{}, false
	}

	text := renderMentions(data.Message, data.Mentions)
	own := in.own()
	if group != "" && in.cfg.RequireMention {
		mentioned := account != "" && strings.Contains(text, "@"+account)
		for _, m := range data.Mentions {
			mentioned = mentioned || (account != "" && m.Number == account) || (own != "" && m.UUID == own)
		}
		if !mentioned {
			return Inbound{}, false
		}
	}
	if group != "" {
		var tags []string
		if account != "" {
			tags = append(tags, "@"+account)
		}
		if own != "" {
			tags = append(tags, "@"+own)
		}
		text = removeMentions(text, tags...)
	}

	var quote *Quote
	if q := data.Quote; q != nil && (q.ID != 0 || q.Text != "") {
		quote = &Quote{ID: q.ID, Text: q.Text, Own: q.ID != 0 && in.isOwn(q.ID)}
	}
	var atts []Attachment
	for _, a := range data.Attachments {
		if a.ID == "" || a.Size > maxAttachment {
			continue
		}
		atts = append(atts, Attachment{ID: a.ID, ContentType: a.ContentType, Filename: a.Filename, Size: a.Size})
	}
	if strings.TrimSpace(text) == "" && len(atts) == 0 {
		return Inbound{}, false
	}

	ts := env.Timestamp
	if ts == 0 {
		ts = data.Timestamp
	}
	out := Inbound{Sender: sender, SenderUUID: env.SourceUUID, SenderName: env.SourceName, Group: group, GroupName: groupName,
		Text: text, Timestamp: ts, Attachments: atts, Quote: quote}
	if group != "" {
		out.To = "group:" + group
		out.Chat = "signal:group:" + group
		if in.cfg.PerUser {
			out.Chat += ":" + sender
		}
	} else {
		out.To = sender
		out.Chat = "signal:dm:" + sender
	}
	return out, true
}

// renderMentions replaces each mention's placeholder with @<number or uuid>.
// Offsets are in UTF-16 code units, as Signal counts them.
func renderMentions(text string, mentions []wireMention) string {
	if len(mentions) == 0 || !strings.Contains(text, objectChar) {
		return text
	}
	u := utf16.Encode([]rune(text))
	sorted := append([]wireMention(nil), mentions...)
	for i := 1; i < len(sorted); i++ { // by start, latest first, so offsets stay valid
		for j := i; j > 0 && sorted[j].Start > sorted[j-1].Start; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	for _, m := range sorted {
		id := m.Number
		if id == "" {
			id = m.UUID
		}
		if id == "" {
			id = "user"
		}
		start := min(max(m.Start, 0), len(u))
		end := min(max(m.Start+max(m.Length, 0), start), len(u))
		repl := utf16.Encode([]rune("@" + id))
		u = append(append(append([]uint16(nil), u[:start]...), repl...), u[end:]...)
	}
	return string(utf16.Decode(u))
}

// removeMentions takes the bot's own @mentions out of a group message, so the
// model does not read its own name as an addressee, and tidies the spacing
// the removal leaves.
func removeMentions(text string, tags ...string) string {
	changed := false
	for _, tag := range tags {
		parts := strings.Split(text, tag)
		if len(parts) == 1 {
			continue
		}
		changed = true
		text = parts[0]
		for _, p := range parts[1:] {
			spaced := strings.HasSuffix(text, " ") || strings.HasPrefix(p, " ")
			left, right := strings.TrimRight(text, " "), strings.TrimLeft(p, " ")
			if spaced && left != "" && right != "" {
				text = left + " " + right
			} else {
				text = left + right
			}
		}
	}
	if !changed {
		return text
	}
	return strings.TrimSpace(text)
}
