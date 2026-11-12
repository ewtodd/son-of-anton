package tui

const spinnerFrames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

func spinnerFrame(i int) string {
	r := []rune(spinnerFrames)
	return string(r[i%len(r)])
}

// toolIcons are one glyph per tool, in column 0 of its row. The second
// block is the tools' names from before they took Claude Code's, which
// stored sessions still carry.
var toolIcons = map[string]string{
	"Bash": "$", "BashOutput": "$", "KillShell": "$", "execute_code": "$",
	"Read": "→", "AskUserQuestion": "→",
	"WebFetch":  "%",
	"WebSearch": "◈",
	"Glob":      "✱", "Grep": "✱", "session_search": "✱",
	"Write": "←", "Edit": "←", "memory": "←",
	"TodoWrite": "☰",
	"cronjob":   "◔",

	"terminal": "$", "process": "$",
	"read_file": "→", "clarify": "→",
	"web_extract":  "%",
	"web_search":   "◈",
	"search_files": "✱",
	"write_file":   "←", "patch": "←",
	"delegate_task": "✓",
	"todo":          "☰",
}

func toolIcon(name string) string {
	if i, ok := toolIcons[name]; ok {
		return i
	}
	return "⚙"
}
