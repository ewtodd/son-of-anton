package tui

import "testing"

// Stored sessions carry calls under the tools' old names and arguments; their
// rows read as sensibly as the new ones.
func TestToolRowsUnderOldAndNewNames(t *testing.T) {
	for _, c := range []struct{ name, args, summary, icon string }{
		{"Bash", `{"command":"go test ./...","description":"Run the tests"}`, "go test ./...", "$"},
		{"terminal", `{"command":"go test ./..."}`, "go test ./...", "$"},
		{"Read", `{"file_path":"/src/a.go"}`, "/src/a.go", "→"},
		{"read_file", `{"path":"/src/a.go"}`, "/src/a.go", "→"},
		{"Edit", `{"file_path":"a.go","old_string":"x","new_string":"y"}`, "a.go", "←"},
		{"patch", `{"path":"a.go","old_string":"x","new_string":"y"}`, "a.go", "←"},
		{"Write", `{"file_path":"b.txt","content":"z"}`, "b.txt", "←"},
		{"write_file", `{"path":"b.txt","content":"z"}`, "b.txt", "←"},
		{"Grep", `{"pattern":"func main"}`, "func main", "✱"},
		{"Glob", `{"pattern":"**/*.go"}`, "**/*.go", "✱"},
		{"search_files", `{"pattern":"func main","target":"content"}`, "func main", "✱"},
		{"BashOutput", `{"bash_id":"bash_ab12"}`, "bash_ab12", "$"},
		{"KillShell", `{"shell_id":"bash_ab12"}`, "kill bash_ab12", "$"},
		{"AskUserQuestion", `{"questions":[{"question":"Which one?"},{"question":"And?"}]}`, "Which one? (+1 more)", "→"},
		{"clarify", `{"question":"Which one?"}`, "Which one?", "→"},
		{"WebFetch", `{"url":"https://example.com","prompt":"p"}`, "https://example.com", "%"},
		{"WebSearch", `{"query":"go modules"}`, "go modules", "◈"},
		{"TodoWrite", `{"todos":[]}`, `{"todos":[]}`, "☰"},
		{"todo", `{"todos":[]}`, `{"todos":[]}`, "☰"},
	} {
		if got := toolSummary(c.name, c.args); got != c.summary {
			t.Errorf("%s summary = %q, want %q", c.name, got, c.summary)
		}
		if got := toolIcon(c.name); got != c.icon {
			t.Errorf("%s icon = %q, want %q", c.name, got, c.icon)
		}
	}
	for name, args := range map[string]string{
		"Task": `{"subagent_type":"Explore","description":"Find it","run_in_background":true}`,
		"task": `{"subagent_type":"explore","description":"Find it","background":true}`,
	} {
		if got := toolSummary(name, args); got != "Explore Task (background) — Find it" {
			t.Errorf("%s row = %q", name, got)
		}
	}
	if got := toolSummary("Task", `{"subagent_type":"general-purpose","description":"Do it"}`); got != "General-Purpose Task — Do it" {
		t.Errorf("general-purpose row = %q", got)
	}
	if !isTask("task") || !isTask("Task") || !isTodo("todo") || !isTodo("TodoWrite") || isTask("TodoWrite") {
		t.Error("isTask and isTodo")
	}
}

// The sidebar reads a TodoWrite result and a todo one stored before it alike.
func TestTheSidebarReadsOldAndNewTodoResults(t *testing.T) {
	m := &model{}
	m.setTodos(`{"todos":[{"content":"run the tests","status":"in_progress","activeForm":"Running the tests"}]}`)
	if len(m.todos) != 1 || m.todos[0].Content != "run the tests" || m.todos[0].Status != "in_progress" {
		t.Fatalf("TodoWrite result: %+v", m.todos)
	}
	m.setTodos(`{"todos":[{"id":"1","content":"old item","status":"cancelled"}],"summary":{"total":1}}`)
	if len(m.todos) != 1 || m.todos[0].Content != "old item" || m.todos[0].Status != "cancelled" {
		t.Fatalf("old todo result: %+v", m.todos)
	}
}
