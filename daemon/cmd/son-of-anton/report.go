package main

// son-of-anton view ... report and sessions report: a session as a PDF
// (daemon/phases/13-reports.md), fetched over the socket and rendered here,
// as the user who runs the command.

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/report"
)

// sessionObj is the part of a session object a report shows.
type sessionObj struct {
	ID      string `json:"id"`
	Kind    string `json:"kind"`
	Title   string `json:"title"`
	Cwd     string `json:"cwd"`
	Model   string `json:"model"`
	Agent   string `json:"agent"`
	Created string `json:"created"`
	Updated string `json:"updated"`
	Usage   struct {
		Input  int `json:"input_tokens"`
		Output int `json:"output_tokens"`
	} `json:"usage"`
}

func (o sessionObj) session() report.Session {
	created, _ := time.Parse(time.RFC3339, o.Created)
	updated, _ := time.Parse(time.RFC3339, o.Updated)
	return report.Session{ID: o.ID, Kind: o.Kind, Title: o.Title, Cwd: o.Cwd, Model: o.Model, Agent: o.Agent,
		Created: created, Updated: updated, InputTokens: o.Usage.Input, OutputTokens: o.Usage.Output}
}

// history is a session's stored messages, oldest first: session.history paged
// back from the newest to the first.
func history(ctx context.Context, c *client.Client, id string) ([]report.Message, error) {
	var msgs []report.Message
	before := int64(0)
	for {
		var res struct {
			Messages   []report.Message `json:"messages"`
			MoreBefore bool             `json:"more_before"`
		}
		if err := c.Call(ctx, "session.history", map[string]any{"session": id, "before_seq": before, "limit": 1000}, &res); err != nil {
			return nil, err
		}
		msgs = append(res.Messages, msgs...)
		if !res.MoreBefore || len(res.Messages) == 0 {
			return msgs, nil
		}
		before = res.Messages[0].Seq
	}
}

// fetch is the session id with its messages and its subagents', down to
// depth levels of subagents.
func fetch(ctx context.Context, c *client.Client, obj sessionObj, depth int) (report.Session, error) {
	s := obj.session()
	var err error
	if s.Messages, err = history(ctx, c, obj.ID); err != nil {
		return s, err
	}
	if depth == 0 {
		return s, nil
	}
	var kids struct {
		Children []sessionObj `json:"children"`
	}
	if err := c.Call(ctx, "session.children", map[string]any{"session": obj.ID}, &kids); err != nil {
		return s, err
	}
	for _, k := range kids.Children {
		child, err := fetch(ctx, c, k, depth-1)
		if err != nil {
			return s, err
		}
		s.Children = append(s.Children, child)
	}
	return s, nil
}

// writeReport renders session id's report to out ("" for ./<id>.pdf, "-" for
// standard output).
func writeReport(ctx context.Context, c *client.Client, id, out string, stdout, stderr io.Writer) int {
	obj := sessionObj{ID: id}
	var listed struct {
		Sessions []sessionObj `json:"sessions"`
	}
	kinds := []string{"interactive", "oneshot", "cron", "child"}
	if err := c.Call(ctx, "session.list", map[string]any{"kinds": kinds, "limit": 1000}, &listed); err != nil {
		return fail(stderr, "%v", err)
	}
	for _, s := range listed.Sessions {
		if s.ID == id {
			obj = s
		}
	}
	s, err := fetch(ctx, c, obj, 3)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	pdf, err := report.Render(ctx, s, time.Local)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	if out == "-" {
		if _, err := stdout.Write(pdf); err != nil {
			return 1
		}
		return 0
	}
	if out == "" {
		out = id + ".pdf"
	}
	if err := os.WriteFile(out, pdf, 0o644); err != nil {
		return fail(stderr, "%v", err)
	}
	fmt.Fprintf(stdout, "wrote %s\n", out)
	return 0
}
