package foreign

import (
	"os"
	"path/filepath"
)

// ListClaude lists the Claude Code session files under root
// (root/*/*.jsonl, one level deep), sorted. A root that is not a directory
// has none.
func ListClaude(root string) ([]string, error) {
	if fi, err := os.Stat(root); err != nil || !fi.IsDir() {
		return nil, nil
	}
	paths, err := filepath.Glob(filepath.Join(root, "*", "*.jsonl"))
	if err != nil {
		return nil, err
	}
	sortPaths(paths)
	return paths, nil
}

// claudeLiveChain is the active branch of a Claude Code transcript. The file
// is appended to forever: a rewind leaves the abandoned branch in it, and a
// compact boundary starts a chain segment with no parentUuid that links back
// through logicalParentUuid. The live conversation is the parent chain that
// ends at the last main-chain record. A file without uuid linkage keeps its
// order.
func claudeLiveChain(objects []map[string]any) []map[string]any {
	byUUID := map[string]map[string]any{}
	for _, obj := range objects {
		if id, _ := str(obj, "uuid"); id != "" {
			if _, seen := byUUID[id]; !seen {
				byUUID[id] = obj
			}
		}
	}
	if len(byUUID) == 0 {
		return objects
	}
	var tip map[string]any
	for i := len(objects) - 1; i >= 0; i-- {
		obj := objects[i]
		if truthy(obj["isSidechain"]) {
			continue
		}
		if id, _ := str(obj, "uuid"); id != "" {
			tip = obj
			break
		}
	}
	if tip == nil {
		return objects
	}
	var chain []map[string]any
	seen := map[string]bool{}
	for cur := tip; cur != nil; {
		id, _ := str(cur, "uuid")
		if id == "" || seen[id] {
			break
		}
		seen[id] = true
		chain = append(chain, cur)
		parent, _ := str(cur, "parentUuid")
		if parent == "" {
			parent, _ = str(cur, "logicalParentUuid")
		}
		cur = nil
		if parent != "" {
			cur = byUUID[parent]
		}
	}
	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain
}

// ParseClaude parses one Claude Code session file.
func ParseClaude(path string) (Session, error) {
	if _, err := os.Stat(path); err != nil {
		return Session{}, err
	}
	objects := readJSONLines(path)
	var (
		turns   []rawTurn
		s       Session
		summary string
		haveCwd bool
		haveID  bool
		u       Usage
	)
	for _, obj := range objects {
		if obj["type"] == "summary" {
			if v, ok := str(obj, "summary"); ok && strip(v) != "" {
				summary = strip(v)
			}
			continue
		}
		if c, ok := str(obj, "cwd"); ok && !haveCwd {
			s.Cwd, haveCwd = c, true
		}
		if id, ok := str(obj, "sessionId"); ok && !haveID {
			s.ID, haveID = id, true
		}
	}
	for _, obj := range claudeLiveChain(objects) {
		if t := obj["type"]; t != "user" && t != "assistant" {
			continue
		}
		if truthy(obj["isSidechain"]) || truthy(obj["isMeta"]) {
			continue
		}
		message, ok := dict(obj["message"])
		if !ok {
			continue
		}
		role, _ := message["role"].(string)
		if role != "user" && role != "assistant" {
			continue
		}
		text := flattenBlocks(message["content"])
		if text == "" || (role == "user" && isWrapperText(text)) {
			continue
		}
		if usage, ok := dict(message["usage"]); ok && role == "assistant" {
			// input_tokens excludes the cache buckets, so the prompt that
			// call saw is input + cache read + cache creation.
			in, read, write := intOr0(usage["input_tokens"]), intOr0(usage["cache_read_input_tokens"]), intOr0(usage["cache_creation_input_tokens"])
			if ctx := in + read + write; ctx > 0 {
				u.LastPrompt = ctx
			}
			u.Input += in
			u.Output += intOr0(usage["output_tokens"])
			u.CacheRead += read
			u.CacheWrite += write
			u.Calls++
		}
		turns = append(turns, rawTurn{role, text, coerceTimestamp(obj["timestamp"])})
	}
	s.Turns = mergeTurns(turns)
	s.Title = summary
	if s.Title == "" {
		s.Title = firstUserLine(turns)
	}
	s.Usage = usageOf(u)
	return s, nil
}
