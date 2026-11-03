// Package nixmerge is the activation-time merges of NixOS and Home Manager,
// ported from nix/config_merge.py and nix/rules_merge.py: the declared
// settings into config.toml, the declared rules into memories/rules.toml,
// each three-way against a state file recording what Nix wrote last.
package nixmerge

import (
	"fmt"
	"io"
	"os"
	"strings"
)

const configUsage = `usage: soa-d config merge DECLARED CONFIG [--state STATE] [--adopt] [--dry-run]
`

// RunConfig runs `soa-d config ...` (args after "config") and returns the
// exit code.
func RunConfig(args []string, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "merge" {
		fmt.Fprint(stderr, configUsage)
		return 2
	}
	var pos []string
	var state string
	var adopt, dry bool
	rest := args[1:]
	for i := 0; i < len(rest); i++ {
		switch a := rest[i]; {
		case a == "--adopt":
			adopt = true
		case a == "--dry-run":
			dry = true
		case a == "--state":
			if i+1 >= len(rest) {
				fmt.Fprint(stderr, configUsage)
				return 2
			}
			i++
			state = rest[i]
		case strings.HasPrefix(a, "--state="):
			state = strings.TrimPrefix(a, "--state=")
		case strings.HasPrefix(a, "--"):
			fmt.Fprint(stderr, configUsage)
			return 2
		default:
			pos = append(pos, a)
		}
	}
	if len(pos) != 2 {
		fmt.Fprint(stderr, configUsage)
		return 2
	}
	if err := mergeConfig(pos[0], pos[1], state, adopt, dry, stderr); err != nil {
		fmt.Fprintf(stderr, "soa-d config merge: %v\n", err)
		return 1
	}
	return 0
}

// mergeConfig is main() of config_merge.py. An error is what the Python
// would die of with a traceback (exit 1).
func mergeConfig(declaredPath, configPath, statePath string, adopt, dryRun bool, stderr io.Writer) error {
	theirs, err := loadTOML(declaredPath)
	if err != nil {
		return err
	}
	ours := newTbl()
	if _, err := os.Stat(configPath); err == nil {
		if ours, err = loadTOML(configPath); err != nil {
			return fmt.Errorf("%s: %w", configPath, err)
		}
	}

	state := loadState(statePath)
	firstRun := state == nil
	base := newTbl()
	if adopt {
		base = deepCopy(ours).(*tbl)
	} else if state != nil {
		base = state
	}

	merged := deepMerge(ours, theirs)
	remove, kept := planRemovals(base, theirs, ours)
	before := deepCopy(merged).(*tbl)
	for _, p := range remove {
		deletePath(merged, p)
	}

	for _, name := range collapse(remove, before, merged) {
		fmt.Fprintf(stderr, "config.toml: removed %s (retired in Nix)\n", name)
	}
	for _, p := range kept {
		fmt.Fprintf(stderr, "config.toml: kept %s — no longer declared in Nix, but its value changed after Nix wrote it\n", render(p))
	}

	if firstRun && !adopt {
		if orphans := unmanagedPaths(ours, theirs); len(orphans) > 0 {
			fmt.Fprintln(stderr, "config.toml: no record of what Nix wrote previously, so nothing "+
				"was retracted on this run. These keys are on disk and not "+
				"declared in Nix — runtime settings, or leftovers from a Nix "+
				"generation that predates this state file:")
			for _, p := range orphans {
				fmt.Fprintf(stderr, "  %s\n", render(p))
			}
			fmt.Fprintln(stderr, "If they are leftovers, re-run this script with --adopt "+
				"(add --dry-run first) to prune them in one pass. Otherwise "+
				"ignore this: from now on, retiring a key in Nix removes it.")
		}
	}

	if dryRun {
		fmt.Fprintln(stderr, "--dry-run: nothing written.")
		return nil
	}

	// Rendered before the file is opened: a value that cannot be written
	// must not leave config.toml truncated.
	text, err := dumpTOML(merged)
	if err != nil {
		return err
	}
	if err := os.WriteFile(configPath, []byte(text), 0o666); err != nil {
		return err
	}
	if statePath != "" {
		js, err := dumpJSON(theirs)
		if err != nil {
			return err
		}
		if err := writeAtomic(statePath, js); err != nil {
			return err
		}
	}
	return nil
}

func deepMerge(base, override *tbl) *tbl {
	result := deepCopy(base).(*tbl)
	for _, k := range override.keys {
		v := override.m[k]
		if cur, ok := result.m[k].(*tbl); ok {
			if ov, ok := v.(*tbl); ok {
				result.set(k, deepMerge(cur, ov))
				continue
			}
		}
		result.set(k, deepCopy(v))
	}
	return result
}

// leafPaths: every path that ends at a non-table value, or at an empty table.
func leafPaths(t *tbl, prefix []string) [][]string {
	var out [][]string
	for _, k := range t.keys {
		path := append(prefix[:len(prefix):len(prefix)], k)
		if sub, ok := t.m[k].(*tbl); ok && len(sub.keys) > 0 {
			out = append(out, leafPaths(sub, path)...)
		} else {
			out = append(out, path)
		}
	}
	return out
}

func getPath(tree *tbl, path []string) (any, bool) {
	var node any = tree
	for _, k := range path {
		t, ok := node.(*tbl)
		if !ok {
			return nil, false
		}
		if node, ok = t.m[k]; !ok {
			return nil, false
		}
	}
	return node, true
}

// deletePath removes path, then any tables it leaves empty.
func deletePath(tree *tbl, path []string) bool {
	type hop struct {
		parent *tbl
		key    string
	}
	var parents []hop
	node := tree
	for _, k := range path[:len(path)-1] {
		next, ok := node.m[k].(*tbl)
		if !ok {
			return false
		}
		parents = append(parents, hop{node, k})
		node = next
	}
	last := path[len(path)-1]
	if _, ok := node.m[last]; !ok {
		return false
	}
	node.del(last)
	for i := len(parents) - 1; i >= 0; i-- {
		h := parents[i]
		if sub, ok := h.parent.m[h.key].(*tbl); ok && len(sub.keys) == 0 {
			h.parent.del(h.key)
		} else {
			break
		}
	}
	return true
}

func render(path []string) string { return strings.Join(path, ".") }

// collapse reports the outermost key that actually disappeared, not every
// leaf.
func collapse(paths [][]string, before, after *tbl) []string {
	var reported []string
	seen := map[string]bool{}
	for _, path := range paths {
		shortest := path
		for stop := 1; stop < len(path); stop++ {
			prefix := path[:stop]
			_, inBefore := getPath(before, prefix)
			_, inAfter := getPath(after, prefix)
			if inBefore && !inAfter {
				shortest = prefix
				break
			}
		}
		// The Python keys this set by the path tuple; "\x00" cannot be in a key
		// that came from a file we parsed, so the join is unambiguous.
		id := strings.Join(shortest, "\x00")
		if !seen[id] {
			seen[id] = true
			reported = append(reported, render(shortest))
		}
	}
	return reported
}

// planRemovals splits the retired paths into (remove, keep-because-edited).
func planRemovals(base, theirs, ours *tbl) (remove, keep [][]string) {
	for _, path := range leafPaths(base, nil) {
		if _, ok := getPath(theirs, path); ok {
			continue
		}
		onDisk, ok := getPath(ours, path)
		if !ok {
			continue
		}
		was, _ := getPath(base, path)
		if pyEq(onDisk, was) {
			remove = append(remove, path)
		} else {
			keep = append(keep, path)
		}
	}
	return remove, keep
}

// unmanagedPaths: on-disk paths Nix does not declare.
func unmanagedPaths(ours, theirs *tbl) [][]string {
	var out [][]string
	for _, p := range leafPaths(ours, nil) {
		if _, ok := getPath(theirs, p); !ok {
			out = append(out, p)
		}
	}
	return out
}
