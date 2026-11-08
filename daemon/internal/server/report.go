package server

// relay report: fiona sends a worker's session to the chat as a PDF
// (daemon/phases/13-reports.md).

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/report"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// reportDepth is how many levels of subagents a report goes down.
const reportDepth = 3

// relayReport renders the worker's report and sends it to the chat; it
// returns once the message has gone, so the result says what happened.
func (s *Server) relayReport(ctx context.Context, stamp string, w store.Session) string {
	rs, err := s.reportSession(ctx, w, reportDepth)
	if err != nil {
		return relayFail("reading the worker: " + err.Error())
	}
	pdf, err := report.Render(ctx, rs, prompt.LocalZone())
	if err != nil {
		return relayFail("rendering the report: " + err.Error())
	}
	name := report.FileName(rs)
	title := w.Title
	if title == "" {
		title = "(untitled)"
	}
	if err := s.chats.state(stamp).sendFile(ctx, "Report: "+title+" ("+w.ID+")", name, "application/pdf", pdf); err != nil {
		return relayFail("sending the report: " + err.Error())
	}
	return relayJSON(map[string]any{"success": true, "sent": name, "title": w.Title, "messages": len(rs.Messages)})
}

// reportSession is a stored session as a report reads it, with its subagents
// down to depth levels.
func (s *Server) reportSession(ctx context.Context, rec store.Session, depth int) (report.Session, error) {
	rs := report.Session{ID: rec.ID, Kind: rec.Kind, Title: rec.Title, Cwd: rec.Cwd, Model: rec.Model, Agent: rec.Agent,
		Created: rec.Created, InputTokens: rec.Usage.Input, OutputTokens: rec.Usage.Output}
	if sum, err := s.st.SessionSummary(ctx, rec.ID); err == nil {
		rs.Updated = sum.Updated
	}
	after := int64(0)
	for {
		rows, more, err := s.st.MessagesAfter(ctx, rec.ID, after, 1000)
		if err != nil {
			return rs, err
		}
		for _, m := range rows {
			rs.Messages = append(rs.Messages, reportMessage(m))
			after = m.Seq
		}
		if !more || len(rows) == 0 {
			break
		}
	}
	if depth == 0 {
		return rs, nil
	}
	kids, err := s.st.Children(ctx, rec.ID)
	if err != nil {
		return rs, err
	}
	for _, k := range kids {
		child, err := s.reportSession(ctx, k, depth-1)
		if err != nil {
			return rs, err
		}
		rs.Children = append(rs.Children, child)
	}
	return rs, nil
}

// reportMessage is a stored row in the shape session.history gives it.
func reportMessage(m store.Message) report.Message {
	out := report.Message{Seq: m.Seq, Role: m.Role, Content: m.Content, Reasoning: m.Reasoning,
		ToolCallID: m.ToolCallID, ToolName: m.ToolName, TS: m.TS.Format(time.RFC3339Nano)}
	for _, img := range m.Images {
		out.Images = append(out.Images, report.Image{ID: img.ID, Mime: img.Mime, Name: img.Name})
	}
	var calls []struct {
		ID       string `json:"id"`
		Function struct {
			Name      string `json:"name"`
			Arguments string `json:"arguments"`
		} `json:"function"`
	}
	if len(m.ToolCalls) > 0 && json.Unmarshal(m.ToolCalls, &calls) == nil {
		for _, c := range calls {
			out.ToolCalls = append(out.ToolCalls, report.ToolCall{ID: c.ID, Name: c.Function.Name, Arguments: c.Function.Arguments})
		}
	}
	return out
}

// sendFile sends text with a file attached to the chat, in order with
// everything else it is sent, and waits until it has gone.
func (cs *chatState) sendFile(ctx context.Context, text, name, mime string, data []byte) error {
	done := make(chan error, 1)
	cs.out.push(func() {
		h := cs.hub
		ts, err := h.cl.SendFile(h.srv.base, cs.to, text, name, mime, data)
		if err != nil {
			log.Printf("signal: sending a file to %s: %v", cs.to, err)
		}
		h.in.Sent(ts)
		done <- err
	})
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		return errors.New("the chat did not take the message in time")
	}
}
