package config

import "strings"

// effortLadder is the canonical ordering of reasoning levels, weakest to
// strongest.
var effortLadder = []string{
	"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
}

func ladderIndex(level string) int {
	for i, l := range effortLadder {
		if l == level {
			return i
		}
	}
	return -1
}

// ClampEffort clamps a requested reasoning effort onto a route's supported
// levels. A level that is supported, or not a ladder level at all, or a
// request against an unknown set passes through unchanged (with its original
// casing). Otherwise the nearest weaker supported level is returned; when
// nothing weaker exists, the weakest one. "none" disables reasoning and is
// never a degradation target for an enabled ask.
func ClampEffort(requested string, supported []string) string {
	req := strings.ToLower(strings.TrimSpace(requested))
	if req == "" || len(supported) == 0 {
		return requested
	}
	var norm []string
	for _, s := range supported {
		l := strings.ToLower(strings.TrimSpace(s))
		if ladderIndex(l) >= 0 {
			norm = append(norm, l)
		}
	}
	if len(norm) == 0 {
		return requested
	}
	for _, l := range norm {
		if l == req {
			return requested
		}
	}
	if ladderIndex(req) < 0 {
		return requested
	}
	var candidates []string
	for _, l := range norm {
		if l != "none" {
			candidates = append(candidates, l)
		}
	}
	if len(candidates) == 0 {
		return requested
	}
	reqIdx := ladderIndex(req)
	best := -1
	worst := -1
	for _, l := range candidates {
		i := ladderIndex(l)
		if i < reqIdx && (best == -1 || i > best) {
			best = i
		}
		if worst == -1 || i < worst {
			worst = i
		}
	}
	if best >= 0 {
		return effortLadder[best]
	}
	return effortLadder[worst]
}
