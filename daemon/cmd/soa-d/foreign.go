package main

// soa-d import --from-claude / --from-codex / --from-opencode: sessions out
// of the other tools' own stores, read-only (daemon/phases/07e-foreign.md).

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/foreign"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// foreignTool is what a --from-* flag reads: the source a session is stored
// under, the label of its title, and the default store.
type foreignTool struct {
	source, label string
}

var foreignTools = map[string]foreignTool{
	"--from-claude":   {"claude-code", "Claude Code"},
	"--from-codex":    {"codex-cli", "Codex CLI"},
	"--from-opencode": {"opencode", "opencode"},
}

// defaultForeignRoot is where the tool keeps its sessions.
func defaultForeignRoot(flag string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	switch flag {
	case "--from-claude":
		return filepath.Join(home, ".claude", "projects"), nil
	case "--from-codex":
		return filepath.Join(home, ".codex", "sessions"), nil
	}
	data := os.Getenv("XDG_DATA_HOME")
	if data == "" {
		data = filepath.Join(home, ".local", "share")
	}
	return filepath.Join(data, "opencode", "opencode.db"), nil
}

// foreignItem is one session to read: a file, or an id in opencode's database.
type foreignItem struct {
	path, id string
}

func importForeign(ctx context.Context, flag, root, home string, force bool, stderr io.Writer) (importResult, error) {
	var res importResult
	tool := foreignTools[flag]
	if root == "" {
		var err error
		if root, err = defaultForeignRoot(flag); err != nil {
			return res, err
		}
	}
	if _, err := os.Stat(root); err != nil {
		return res, err
	}
	var items []foreignItem
	switch flag {
	case "--from-claude", "--from-codex":
		list := foreign.ListClaude
		if flag == "--from-codex" {
			list = foreign.ListCodex
		}
		paths, err := list(root)
		if err != nil {
			return res, err
		}
		for _, p := range paths {
			items = append(items, foreignItem{path: p})
		}
	default:
		ids, err := foreign.ListOpencode(root)
		if err != nil {
			return res, fmt.Errorf("reading %s: %w", root, err)
		}
		for _, id := range ids {
			items = append(items, foreignItem{path: root, id: id})
		}
	}

	cfg, err := config.Load(home)
	if err != nil {
		return res, err
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		return res, err
	}
	defer st.Close()
	// A new interactive session's prompt, built once per directory.
	now := time.Now().In(prompt.LocalZone())
	prompts := map[string]string{}
	promptFor := func(cwd string) (string, error) {
		if text, ok := prompts[cwd]; ok {
			return text, nil
		}
		r, err := prompt.Build(prompt.Options{
			Home: home, Cwd: cwd, Scope: "common", Memory: true,
			Now: now, RulesCharLimit: cfg.Memory.RulesCharLimit,
		})
		for _, w := range r.Warnings {
			fmt.Fprintf(stderr, "soa-d: %s\n", w)
		}
		prompts[cwd] = r.Text
		return r.Text, err
	}

	for _, it := range items {
		if it.id != "" && !force {
			if known, err := st.ForeignImported(ctx, tool.source, it.id); err != nil {
				return res, err
			} else if known {
				res.present++
				continue
			}
		}
		var fs foreign.Session
		var err error
		switch flag {
		case "--from-claude":
			fs, err = foreign.ParseClaude(it.path)
		case "--from-codex":
			fs, err = foreign.ParseCodex(it.path)
		default:
			fs, err = foreign.ParseOpencode(it.path, it.id)
		}
		if err != nil {
			fmt.Fprintf(stderr, "soa-d import: skipping %s: %v\n", it.path, err)
			res.skipped++
			continue
		}
		if len(fs.Turns) == 0 {
			res.skipped++
			continue
		}
		fid := fs.ID
		if fid == "" {
			fid = it.path
		}
		text, err := promptFor(fs.Cwd)
		if err != nil {
			return res, err
		}
		sess, msgs := foreignSession(tool, it, fs)
		present, err := st.ImportSession(ctx, sess, text, msgs, store.ImportOpts{
			Foreign: &store.Foreign{Source: tool.source, ID: fid}, Force: force,
		})
		if err != nil {
			return res, fmt.Errorf("importing %s: %w", it.path, err)
		}
		if present {
			res.present++
			continue
		}
		res.imported++
		res.messages += len(msgs)
	}
	return res, nil
}

// foreignSession is the soa-d session and rows for a parsed foreign one.
func foreignSession(tool foreignTool, it foreignItem, fs foreign.Session) (store.Session, []store.Message) {
	created := time.Now()
	for _, t := range fs.Turns {
		if t.Timestamp != nil {
			created = unixTime(*t.Timestamp)
			break
		}
	}
	sess := store.Session{
		Kind: "interactive", Source: tool.source, Title: foreignTitle(tool.label, it, fs), TitleSource: "user",
		Cwd: fs.Cwd, Scope: "common", Created: created,
		Usage:         store.Usage{Input: fs.Usage.Input, Output: fs.Usage.Output, Cached: fs.Usage.CacheRead, Reasoning: fs.Usage.Reasoning, Calls: fs.Usage.Calls},
		ContextTokens: fs.Usage.LastPrompt,
	}
	if fs.Cwd != "" {
		sess.Workspace = paths.Workspace(fs.Cwd)
	}
	msgs := make([]store.Message, len(fs.Turns))
	for i, t := range fs.Turns {
		// A turn with no time (the stub before a leading assistant reply)
		// is stamped with the session's.
		msgs[i] = store.Message{Role: t.Role, Content: t.Content, TS: created}
		if t.Timestamp != nil {
			msgs[i].TS = unixTime(*t.Timestamp)
		}
	}
	return sess, msgs
}

// foreignTitle is "Imported from <tool>: <seed>": the title guess's first
// line, else the first user line, else the file's stem; over 60 characters
// it is cut to 59 and an ellipsis.
func foreignTitle(label string, it foreignItem, fs foreign.Session) string {
	seed := strings.TrimSpace(fs.Title)
	if seed != "" {
		seed = strings.TrimSpace(strings.SplitN(strings.NewReplacer("\r\n", "\n", "\r", "\n").Replace(seed), "\n", 2)[0])
	}
	if seed == "" {
		for _, t := range fs.Turns {
			if t.Role == "user" {
				seed = strings.TrimSpace(strings.SplitN(strings.TrimSpace(t.Content), "\n", 2)[0])
				break
			}
		}
	}
	if seed == "" {
		seed = strings.TrimSuffix(filepath.Base(it.path), filepath.Ext(it.path))
	}
	if r := []rune(seed); len(r) > 60 {
		seed = string(r[:59]) + "…"
	}
	return "Imported from " + label + ": " + seed
}
