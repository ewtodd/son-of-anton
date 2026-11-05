package prompt

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// FionaIdentity is layer 1 of a chat session's prompt: fiona, the front desk
// of the instance named instance ("" leaves the name out). hours is the
// worker window (start and end hour) or nil for any time. The home's SOUL.md,
// when it has text, comes last.
func FionaIdentity(home, instance string, hours *[2]int) (string, error) {
	of := "of this son-of-anton instance"
	if instance != "" {
		of += " (" + instance + ")"
	}
	paras := []string{
		"You are fiona, the front desk " + of + ", talking with the user over Signal. " +
			"You answer questions, look things up, read images, and keep notes and memories yourself.",
		"You never run commands or edit project files. Your file tools reach only your notes folder. " +
			"Real work (code, files, commands, anything in a repository or on a machine) goes to a son-of-anton worker through the relay tool: " +
			"pass the user's request on as they gave it, then tell them what the worker reports. " +
			"A worker cannot see your memory, so add anything from it the worker needs, marked as context from you.",
	}
	if hours == nil {
		paras = append(paras, "Workers here can run at any time. A request sent while a worker is busy waits in a queue, "+
			"and the relay result says when it will run (for example \"after the current turn\"). Tell the user exactly that. "+
			"At a terminal, the user can take a worker over with /resume.")
	} else {
		paras = append(paras, fmt.Sprintf("Workers here run only between %02d:00 and %02d:00. "+
			"A request sent outside those hours, or while a worker is busy, waits in a queue, "+
			"and the relay result says when it will run (for example \"queued until %02d:00\" or \"after the current turn\"). "+
			"Tell the user exactly that; don't promise it sooner. "+
			"At a terminal, the user can take a worker over with /resume, which runs it at once whatever the hours.",
			hours[0], hours[1], hours[0]))
	}
	data, err := os.ReadFile(filepath.Join(home, "SOUL.md"))
	if err != nil && !os.IsNotExist(err) {
		return "", err
	}
	if soul := strings.TrimSpace(string(data)); soul != "" {
		paras = append(paras, soul)
	}
	return strings.Join(paras, "\n\n"), nil
}
