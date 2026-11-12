package tools

// Edit's diff renderer, exercised on the edit script directly: the
// results the model reads must show each changed and context line exactly
// once.

import (
	"strings"
	"testing"
)

func hunkCount(d string) int {
	n := 0
	for _, l := range strings.Split(d, "\n") {
		if strings.HasPrefix(l, "@@") {
			n++
		}
	}
	return n
}

// Two changes less than 2*ctx lines apart share one hunk, and the context
// between them appears exactly once.
func TestUnifiedDiffMergesNearbyChangesWithoutDuplicatingContext(t *testing.T) {
	old := "a1\na2\na3\na4\na5\na6\na7\na8\na9\na10\n"
	new := "a1\na2\nb3\na4\na5\na6\nb7\na8\na9\na10\n"
	d := unifiedDiff("f.txt", old, new)
	if hunkCount(d) != 1 {
		t.Fatalf("want a single hunk:\n%s", d)
	}
	for _, line := range []string{" a4\n", " a5\n", " a6\n"} {
		if strings.Count(d, line) != 1 {
			t.Fatalf("context %q appears %d times:\n%s", line, strings.Count(d, line), d)
		}
	}
	for _, line := range []string{"-a3\n", "+b3\n", "-a7\n", "+b7\n"} {
		if strings.Count(d, line) != 1 {
			t.Fatalf("%q appears %d times:\n%s", line, strings.Count(d, line), d)
		}
	}
}

// Changes 2*ctx lines apart stay separate hunks.
func TestUnifiedDiffKeepsDistantChangesSeparate(t *testing.T) {
	old := "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\nl11\nl12\nl13\nl14\nl15\nl16\nl17\nl18\nl19\nl20\n"
	new := "l1\nl2-new\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\nl11\nl12\nl13\nl14\nl15\nl16\nl17\nl18-new\nl19\nl20\n"
	d := unifiedDiff("f.txt", old, new)
	if hunkCount(d) != 2 {
		t.Fatalf("want two hunks:\n%s", d)
	}
}
