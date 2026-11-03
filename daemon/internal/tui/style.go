package tui

const spinnerFrames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

func spinnerFrame(i int) string {
	r := []rune(spinnerFrames)
	return string(r[i%len(r)])
}

// toolIcons are one glyph per tool, in column 0 of its row.
var toolIcons = map[string]string{
	"terminal": "$", "execute_code": "$",
	"read_file": "→", "clarify": "→",
	"web_extract":  "%",
	"web_search":   "◈",
	"search_files": "✱", "session_search": "✱",
	"write_file": "←", "patch": "←", "memory": "←",
	"delegate_task":  "✓",
	"todo":           "☰",
	"cronjob":        "◔",
	"vision_analyze": "◉",
}

func toolIcon(name string) string {
	if i, ok := toolIcons[name]; ok {
		return i
	}
	return "⚙"
}
