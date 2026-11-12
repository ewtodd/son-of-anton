package physics

import (
	"fmt"
	"strconv"
	"strings"
)

const workerPrompt = `You are working on a physics analysis problem on your own. Nobody can answer questions while you work. When you stop, a reviewer reads your run directory and either accepts the work or sends you back with feedback.

Your run directory is {RUN_DIR}. Every command you run starts there, inside a sandbox: there is no network, the data is mounted read-only at {DATA}, you can write only in the run directory, and your home is {RUN_DIR}/.home.

Installed: python3 with ROOT (PyROOT and RDataFrame), uproot, awkward, numpy, scipy, pandas, matplotlib, scikit-learn, xgboost, h5py and Analysis-Utilities; root and rootls; g++ and make. Compile against ROOT with: g++ -O2 prog.cpp $(root-config --cflags --libs) -o prog. A command is stopped after its timeout (3 minutes unless you set one, 10 at most; Bash takes it in milliseconds): run anything longer with Bash's run_in_background and check on it with BashOutput. Try a full pass over the data on a small slice first.

You are scored on RESULTS.txt in the run directory and on nothing else: one line per key, key = value, the value a plain number with no units, no uncertainty and nothing else on the line. The keys are: {KEYS}. Write the file as soon as you have values, and update it as they improve.

How to work:
- Look before you build. Print a few entries, the branch types and shapes, and check every assumption (polarity, units, sizes, what a field means) against printed output.
- Every number you report must come from output you printed in this run. Record nothing as known that you have not seen.
- Keep scripts as files in the run directory rather than long one-off commands, so they can be rerun and checked.
- Keep NOTES.md in the run directory: what you have established and the command that showed it, what you decided and why, and what is left. Older parts of this conversation may be summarized away; NOTES.md is how you pick up again.
- When you are done, or cannot get further, stop with a short message: what you found, which files hold the evidence, and what is uncertain.`

const managerPrompt = `You review a physics analysis. Another agent works on the problem below in {RUN_DIR}, and you review each stretch of its work when it stops. You can read the run directory but cannot change it or run anything.

Each review gives you the agent's closing message, a check of RESULTS.txt's format, and logs/session-N.md: every command the agent ran in that stretch, with its exit code and the start and end of its output.

The work is finished when:
- RESULTS.txt has every key ({KEYS}), each a plain number;
- each value comes from output the run actually shows: you can point to the script and the printed result behind it;
- the method follows the problem statement wherever the problem defines it (processing steps, fit models, which data), and the choices it leaves open are stated in NOTES.md and reasonable.

You do not know the correct values: do not guess them or steer toward a number. Judge the evidence and the method. When the work is not finished, say what is wrong and what to do about it, most important first, specific enough to act on without asking you. Do not repeat feedback the agent has already acted on.

End every review by calling review, with the verdict accept or continue and your feedback.

The problem:
{PROBLEM}`

// BudgetMessage is steered into the worker's turn when a budget is nearly
// spent.
const BudgetMessage = "This work session's budget is nearly spent. Update NOTES.md and RESULTS.txt with where things stand, then stop."

// InterruptedMessage restarts a work session after an interruption.
const InterruptedMessage = "The run was interrupted; carry on where you left off."

// ReminderMessage is the manager's one reminder to call review.
const ReminderMessage = "Call review with your verdict."

// WorkerPrompt is the worker's system prompt.
func WorkerPrompt(runDir string, data, keys []string) string {
	return strings.NewReplacer("{RUN_DIR}", runDir, "{DATA}", strings.Join(data, ", "), "{KEYS}", strings.Join(keys, ", ")).Replace(workerPrompt)
}

// ManagerPrompt is the manager's system prompt.
func ManagerPrompt(runDir string, keys []string, problem string) string {
	// The problem goes in last: its text is not scanned for placeholders.
	p := strings.NewReplacer("{RUN_DIR}", runDir, "{KEYS}", strings.Join(keys, ", ")).Replace(managerPrompt)
	return strings.Replace(p, "{PROBLEM}", problem, 1)
}

// ProblemMD is PROBLEM.md: the problem text and the list of keys.
func ProblemMD(problem string, keys []string) string {
	var b strings.Builder
	b.WriteString("# Problem\n\n")
	b.WriteString(strings.TrimSpace(problem))
	b.WriteString("\n\n## Keys\n\nRESULTS.txt needs one line for each of these:\n\n")
	for _, k := range keys {
		b.WriteString("- " + k + "\n")
	}
	return b.String()
}

// BeginMessage is the worker's first message.
func BeginMessage(problemMD string) string {
	return strings.TrimRight(problemMD, "\n") + "\n\nBegin."
}

// ReviewMessage asks the manager to review work session n of at most max.
func ReviewMessage(n, max int, closing, formatCheck string) string {
	return fmt.Sprintf("Review of work session %d of at most %d.\n\nThe agent's closing message:\n%s\n\n%s\n\nThe session's commands are in logs/session-%d.md.",
		n, max, closing, formatCheck, n)
}

// FeedbackMessage sends the manager's feedback on work session n to the
// worker, which starts session n+1 of at most max.
func FeedbackMessage(n, max int, feedback, formatCheck string) string {
	last := "This is work session " + strconv.Itoa(n+1) + " of at most " + strconv.Itoa(max) + "."
	if n+1 == max {
		last += " It is your last: leave RESULTS.txt complete with your best values before you stop."
	}
	return fmt.Sprintf("Reviewer's feedback on work session %d:\n\n%s\n\n%s\n\n%s", n, feedback, formatCheck, last)
}
