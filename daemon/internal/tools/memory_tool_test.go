package tools_test

// Phase 3b: the memory tool (ported from ../tools/memory_tool.py). Every rule
// change asks the user, whatever the permission mode; facts are written at
// once unless memory.write_approval is on; nobody to ask means no change.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// memFixture is a tool fixture with a memory store and a recording approver.
func memFixture(t *testing.T, mode approval.Mode, answer approval.Choice) (*fixture, *[]approval.Request) {
	t.Helper()
	var asked []approval.Request
	approver := func(_ context.Context, req approval.Request) (approval.Choice, error) {
		asked = append(asked, req)
		return answer, nil
	}
	f := setup(t, approval.NewGate(mode, approver, nil))
	f.env.Memory = memory.New(f.env.Policy.SoaHome)
	f.env.MemoryScope = "common"
	return f, &asked
}

func rulesFile(t *testing.T, f *fixture) string {
	t.Helper()
	b, _ := os.ReadFile(filepath.Join(f.env.Policy.SoaHome, "memories", "rules.toml"))
	return string(b)
}

func TestTheMemorySchemaIsThePythonOne(t *testing.T) {
	tool, ok := tools.NewRegistry(tools.Builtin()...).Lookup("memory")
	if !ok {
		t.Fatal("memory is a builtin tool")
	}
	s := tool.Schema()
	var params struct {
		Properties map[string]struct {
			Enum []string `json:"enum"`
		} `json:"properties"`
		Required []string `json:"required"`
	}
	if err := json.Unmarshal(s.Function.Parameters, &params); err != nil {
		t.Fatal(err)
	}
	for _, phrase := range []string{
		"A rule (kind='rule') is a standing instruction or core context that must hold every turn",
		"so there is no read action",
		"A full rules store refuses: ask the user which rule to retire.",
		"rules.toml, facts.toml, facts.gateway.toml",
	} {
		if !strings.Contains(s.Function.Description, phrase) {
			t.Errorf("description lacks %q", phrase)
		}
	}
	if strings.Join(params.Properties["kind"].Enum, ",") != "rule,fact" ||
		strings.Join(params.Properties["action"].Enum, ",") != "add,replace,remove" ||
		strings.Join(params.Properties["scope"].Enum, ",") != "current,common,gateway" ||
		strings.Join(params.Required, ",") != "kind,action" {
		t.Fatalf("parameters = %s", s.Function.Parameters)
	}
}

func TestARuleChangeAsksTheUserEvenInYolo(t *testing.T) {
	f, asked := memFixture(t, approval.Yolo, approval.Once)
	r := f.call(t, "memory", map[string]any{"kind": "rule", "action": "add", "text": "Answer in plain English", "why": "readability"})
	if r["success"] != true || str(r, "id") != "answer-in-plain-english" || str(r, "kind") != "rule" || str(r, "scope") != "common" ||
		!strings.Contains(str(r, "usage"), "/6,000 chars") || str(r, "note") != "Saved. Nothing more is needed for this change." {
		t.Fatalf("result = %v", r)
	}
	if len(*asked) != 1 {
		t.Fatalf("approvals asked = %d; a rule always asks, even in yolo", len(*asked))
	}
	req := (*asked)[0]
	if req.Kind != "memory" || !strings.Contains(req.Subject, "Answer in plain English") || !strings.Contains(req.Subject, "why: readability") ||
		len(req.Reasons) == 0 || req.Reasons[0] != "Memory change: add rule (common)" || req.AllowRemember {
		t.Fatalf("request = %+v", req)
	}
	if !strings.Contains(rulesFile(t, f), "Answer in plain English") {
		t.Fatal("an approved rule is written")
	}
}

func TestADeniedRuleIsNotWritten(t *testing.T) {
	f, _ := memFixture(t, approval.Default, approval.Deny)
	r := f.call(t, "memory", map[string]any{"kind": "rule", "action": "add", "text": "Never do anything"})
	if r["success"] != false || str(r, "error") != "The user refused this memory change, so nothing was saved." {
		t.Fatalf("result = %v", r)
	}
	if rulesFile(t, f) != "" {
		t.Fatal("nothing written")
	}
}

func TestNobodyToAskMeansNoRuleChange(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	f.env.Memory = memory.New(f.env.Policy.SoaHome)
	f.env.MemoryScope = "common"
	r := f.call(t, "memory", map[string]any{"kind": "rule", "action": "add", "text": "Always use tabs"})
	if r["success"] != false || !strings.Contains(strings.ToLower(str(r, "error")), "nobody") {
		t.Fatalf("result = %v; with no one to approve, a rule change is refused and says why", r)
	}
	// Facts need no approval by default.
	if r := f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "The printer is in the attic"}); r["success"] != true {
		t.Fatalf("fact = %v", r)
	}
}

func TestFactsAskOnlyWhenWriteApprovalIsOn(t *testing.T) {
	f, asked := memFixture(t, approval.Default, approval.Once)
	f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "The printer is in the attic"})
	if len(*asked) != 0 {
		t.Fatal("write_approval off: a fact is written without asking")
	}
	f.env.MemoryApproval = true
	r := f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "The router lives in the closet"})
	if r["success"] != true || len(*asked) != 1 || (*asked)[0].Reasons[0] != "Memory change: add fact (common)" {
		t.Fatalf("write_approval on: %v, asked %+v", r, *asked)
	}
}

func TestAnInvalidChangeNeverAsks(t *testing.T) {
	f, asked := memFixture(t, approval.Default, approval.Once)
	r := f.call(t, "memory", map[string]any{"kind": "rule", "action": "replace", "id": "missing", "text": "x"})
	if r["success"] != false || !strings.Contains(str(r, "error"), "No rule with id 'missing'") || len(*asked) != 0 {
		t.Fatalf("result = %v, asked %d", r, len(*asked))
	}
	r = f.call(t, "memory", map[string]any{"kind": "note", "action": "add", "text": "x"})
	if !strings.Contains(str(r, "error"), "Invalid kind 'note'") {
		t.Fatalf("result = %v", r)
	}
	r = f.call(t, "memory", map[string]any{"kind": "fact", "action": "read"})
	if !strings.Contains(str(r, "error"), "There is no read action") {
		t.Fatalf("result = %v", r)
	}
}

func TestWithoutAStoreMemoryIsUnavailable(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "x"})
	if r["success"] != false || !strings.Contains(str(r, "error"), "Memory is off for this session") {
		t.Fatalf("result = %v", r)
	}
}

func TestANearDuplicateNamesTheExistingFact(t *testing.T) {
	f, _ := memFixture(t, approval.Default, approval.Once)
	first := f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "The NAS lives at 10.0.0.3 on the lan"})
	r := f.call(t, "memory", map[string]any{"kind": "fact", "action": "add", "text": "the NAS lives at 10.0.0.3, on the LAN"})
	sim, _ := r["similar"].([]any)
	if r["success"] != false || len(sim) != 1 || str(sim[0].(map[string]any), "id") != str(first, "id") {
		t.Fatalf("result = %v", r)
	}
}
