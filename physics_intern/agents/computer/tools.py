"""Tool execution for agentic agents (execute_python via sandbox)."""

import re
import time
from pathlib import Path
from typing import ClassVar

from physics_intern.utils.sandbox import (
    SandboxPolicy,
    describe_runtime,
    execute_python,
    runtime_guidance,
)
from physics_intern.state.task import TaskType
from physics_intern.state.tool_call import ToolCall  # noqa: F401 — re-export for backward compat


class ToolExecutor:
    """Dispatches tool calls for agentic agents.

    The LLM never chooses file paths — it passes code as a string, and
    ToolExecutor writes it to computations/tool_exec_NNN.py.
    """

    @staticmethod
    def _execute_python_def(policy: "SandboxPolicy | None" = None) -> dict:
        """Build the execute_python schema against the real runtime.

        The description is generated, not hardcoded: it names the packages the
        configured interpreter can actually import, and the directories the
        sandbox actually exposes. A hardcoded list is how the tool came to
        advertise NumPy/SciPy/SymPy/matplotlib to an interpreter that has none
        of them.
        """
        interpreter = policy.interpreter if policy else None
        data_note = ""
        if policy and policy.data_dirs:
            listed = ", ".join(str(d) for d in policy.data_dirs)
            data_note = (
                f"\n\nRead-only data available at: {listed}. "
                "These paths are mounted read-only; write everything you "
                "produce into the working directory instead."
            )
        guidance = runtime_guidance(interpreter)
        if guidance:
            guidance = "\n\n" + guidance
        return {
            "type": "function",
            "function": {
                "name": "execute_python",
                "description": (
                    "Execute a Python script and return its stdout/stderr.\n\n"
                    f"Available: {describe_runtime(interpreter)}\n\n"
                    "BANNED APIs (will crash):\n"
                    "- scipy.misc.derivative -> manual finite differences\n"
                    "- numpy.trapz -> numpy.trapezoid\n"
                    "- numpy.math -> math (stdlib)\n"
                    "- scipy.integrate.simps -> scipy.integrate.simpson\n\n"
                    "The script runs in a sandbox with no network access and "
                    "no access to the home directory. The working directory is "
                    "writable and persists between calls; everything else is "
                    "discarded when the script exits."
                    f"{data_note}\n\n"
                    "The script must be self-contained. Never call plt.show() "
                    "(use plt.savefig() then plt.close()). "
                    "Timeout: scripts are killed after the configured timeout "
                    "(default 60s). If you hit a timeout, simplify your "
                    "approach: reduce grid sizes, use fewer iterations, or "
                    "switch to analytical methods."
                    f"{guidance}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "purpose": {
                            "type": "string",
                            "description": (
                                "What this script computes and what you expect "
                                "to learn from the output. Be specific: state "
                                "the quantity being computed, the method used, "
                                "and how the result advances toward the "
                                "deliverable."
                            ),
                        },
                        "code": {
                            "type": "string",
                            "description": "The complete Python script to execute.",
                        },
                        "filename": {
                            "type": "string",
                            "description": (
                                "A short, descriptive filename for this script "
                                "(e.g. 'verify_enumeration.py'). Each script "
                                "runs as an independent .py file."
                            ),
                        },
                    },
                    "required": ["purpose", "code"],
                },
            },
        }

    _SUBMIT_RESULT_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "submit_result",
            "description": (
                "Submit the result of an exploratory computation. Call this ONCE "
                "when you have a concrete result. This immediately ends your session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The RQ/WH/ER ID being explored (e.g. 'WH-001'). Use the target from your task assignment.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What was computed.",
                    },
                    "method": {
                        "type": "string",
                        "description": "Approach used.",
                    },
                    "result": {
                        "type": "string",
                        "description": "The actual result.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["exact", "approximate", "partial"],
                        "description": "Confidence level of the result.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes.",
                    },
                    "evidence_scripts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of script filenames that produced meaningful "
                            "results supporting your conclusion. Ideally only ONE script should contain all the evidence."
                            "Exclude scripts that superfluous, errored, timed out, were abandoned before "
                            "completing, or produced clearly incorrect output. "
                            "Only the listed scripts will be shown to the reviewer. Aim for one unless necessary."
                        ),
                    },
                },
                "required": [
                    "target_id",
                    "description",
                    "method",
                    "result",
                    "confidence",
                    "notes",
                ],
            },
        },
    }

    _REPORT_PROGRESS_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "report_progress",
            "description": (
                "Report your progress so far. You MUST call this when prompted by "
                "the system before making more execute_python calls. Summarize what "
                "your computations have shown and whether you have enough evidence "
                "to reach a conclusion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "findings_so_far": {
                        "type": "string",
                        "description": "Summary of what your computations have shown so far.",
                    },
                    "remaining_questions": {
                        "type": "string",
                        "description": "What specific new information you still need, if any.",
                    },
                    "ready_to_conclude": {
                        "type": "boolean",
                        "description": "True if you have enough evidence to call submit_result.",
                    },
                },
                "required": [
                    "findings_so_far",
                    "remaining_questions",
                    "ready_to_conclude",
                ],
            },
        },
    }

    _DOCUMENT_APPROACH_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "document_approach",
            "description": (
                "Document your computational approach BEFORE writing code. "
                "You MUST call this before your first execute_python call. "
                "Records your plan, assumptions, and expected outcome so "
                "the verifier can later assess your methodology."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "approach": {
                        "type": "string",
                        "description": (
                            "Detailed description of the computational approach: "
                            "what you will compute, how, and why this method is appropriate."
                        ),
                    },
                    "assumptions": {
                        "type": "string",
                        "description": "Assumptions underlying the computation.",
                    },
                    "expected_outcome": {
                        "type": "string",
                        "description": "What form the result should take and how to judge success.",
                    },
                },
                "required": ["approach"],
            },
        },
    }

    # Tool sets by agent type. These are built per instance rather than as
    # ClassVars: the execute_python schema names the packages and data mounts
    # of the sandbox this executor was constructed with, which is not known at
    # import time.
    def computer_tools(self) -> list[dict]:
        """Full computer tool set, with the runtime-derived execute_python."""
        return [
            self._DOCUMENT_APPROACH_DEF,
            self._execute_python_def(self._policy),
            self._SUBMIT_RESULT_DEF,
        ] + self._lookup_tools()

    def _lookup_tools(self) -> list[dict]:
        """Documentation lookups, when this role is configured for them."""
        return self._mcp.tools_for("computer") if self._mcp else []

    def tools_for_task_type(self, task_type: "TaskType") -> list[dict]:
        """Return the appropriate tool set for a task type."""
        if task_type == TaskType.COMPUTE:
            return self.computer_tools()
        return self.computer_tools()  # default fallback

    # Dynamic tool sets for computer agent lifecycle
    _COMPUTER_TOOLS_INITIAL: ClassVar[list[dict]] = [
        _DOCUMENT_APPROACH_DEF,
        _SUBMIT_RESULT_DEF,
    ]

    def _computer_tools_post_approach(self) -> list[dict]:
        return [
            self._execute_python_def(self._policy),
            self._SUBMIT_RESULT_DEF,
        ] + self._lookup_tools()

    def _computer_tools_progress(self) -> list[dict]:
        return [
            self._execute_python_def(self._policy),
            self._SUBMIT_RESULT_DEF,
            self._REPORT_PROGRESS_DEF,
        ] + self._lookup_tools()

    def __init__(
        self,
        workspace_root: Path,
        timeout: int = 60,
        output_limit: int = 10_000,
        task_type: "TaskType | None" = None,
        policy: "SandboxPolicy | None" = None,
        progress_check_interval: int = 0,
        mcp=None,
    ):
        self.workspace_root = workspace_root
        # The workspace root — not the computations subdirectory — is the
        # sandbox's writable mount: problem specs tell the model to write
        # RESULTS.txt where the formal evaluator reads it, at the root.
        self._policy = policy or SandboxPolicy.from_config()
        self._mcp = mcp
        if self._policy.workspace is None:
            self._policy.workspace = Path(workspace_root)
        self.timeout = timeout
        self._output_limit = output_limit
        self._counter = 0
        self._progress_check_interval = progress_check_interval
        self._computations_dir = workspace_root / "computations"
        self._task_type = task_type
        self.ready_to_conclude_signaled = False
        self._script_names: list[str] = []
        self._approach_documented: bool = False
        self._progress_check_pending: bool = False

    @staticmethod
    def _sanitize_filename(raw: str, max_len: int = 60) -> str:
        """Sanitize a model-provided filename for safe filesystem use."""
        # Strip path separators and parent references
        cleaned = raw.replace("/", "_").replace("\\", "_").replace("..", "_")
        # Remove non-alphanumeric except _, -, .
        cleaned = re.sub(r"[^a-zA-Z0-9_.\-]", "", cleaned)
        # Ensure .py extension
        if not cleaned.endswith(".py"):
            cleaned = re.sub(r"\.[^.]*$", "", cleaned)  # strip wrong extension
            cleaned += ".py"
        # Truncate (keep .py suffix)
        if len(cleaned) > max_len:
            cleaned = cleaned[: max_len - 3] + ".py"
        return cleaned or "script.py"

    @property
    def exit_tool_name(self) -> str:
        """Return the context-appropriate exit tool name."""
        return "submit_result"

    @property
    def exit_tool_names(self) -> frozenset[str]:
        """Return all exit tool names (for multi-exit-tool executors)."""
        return frozenset({self.exit_tool_name})

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        """Dispatch a tool call by name."""
        start = time.time()

        if tool_name == "execute_python":
            if "code" not in tool_input:
                output = (
                    "ERROR: Missing required 'code' parameter. "
                    "execute_python requires a 'code' field containing "
                    "the complete Python script to run."
                )
                is_error = True
            else:
                output, is_error = self._execute_python(
                    tool_input["code"],
                    purpose=tool_input.get("purpose", ""),
                    filename=tool_input.get("filename", ""),
                )
        elif tool_name == "submit_result":
            output, is_error = self._submit_result(tool_input)
        elif tool_name == "document_approach":
            output, is_error = self._document_approach(tool_input)
        elif tool_name == "report_progress":
            output, is_error = self._report_progress(tool_input)
        elif self._mcp is not None and self._mcp.handles(tool_name):
            output, is_error = self._mcp.call(tool_name, tool_input)
        else:
            output = (
                f"ERROR: Unknown tool '{tool_name}'. "
                f"Available tools: execute_python, submit_result, "
                f"document_approach, report_progress."
            )
            is_error = True

        duration = time.time() - start
        return ToolCall(
            tool_name=tool_name,
            tool_input=tool_input,
            output=output,
            is_error=is_error,
            duration=duration,
        )

    def _document_approach(self, params: dict) -> tuple[str, bool]:
        """Record the computational approach before coding. Only callable once."""
        if self._approach_documented:
            return (
                "Error: approach already documented. "
                "Call execute_python to run your code, or submit_result to finish."
            ), True
        approach = params.get("approach", "")
        assumptions = params.get("assumptions", "")
        expected_outcome = params.get("expected_outcome", "")
        self._documented_approach = {
            "approach": approach,
            "assumptions": assumptions,
            "expected_outcome": expected_outcome,
        }
        self._approach_documented = True
        return "Approach documented. Now call execute_python to run your code.", False

    @property
    def active_tools(self) -> list[dict] | None:
        """Return dynamic tool set, or None to keep the original tools.

        Computer agent lifecycle:
        - Before document_approach: only [document_approach, submit_result]
        - After document_approach: [execute_python, submit_result]
        - During progress check: adds report_progress temporarily
        """
        if not self._approach_documented:
            return self._COMPUTER_TOOLS_INITIAL
        if self._progress_check_pending:
            return self._computer_tools_progress()
        return self._computer_tools_post_approach()

    def _report_progress(self, params: dict) -> tuple[str, bool]:
        """Acknowledge progress report and guide next action."""
        self._progress_check_pending = False
        exit_tool = self.exit_tool_name
        ready = params.get("ready_to_conclude", False)
        if ready:
            self.ready_to_conclude_signaled = True
            return (
                "Acknowledged. You have indicated you are ready to conclude. "
                f"Call `{exit_tool}` now with your findings."
            ), False
        remaining = params.get("remaining_questions", "")
        return (
            f"Acknowledged. Remaining questions: {remaining}\n"
            "Continue with your next execute_python call, then call "
            f"{exit_tool} when you have enough evidence."
        ), False

    def _submit_result(self, params: dict) -> tuple[str, bool]:
        """Record exploratory result and signal loop to stop."""
        self.stop_after_round = True
        self._last_result = params
        target = params.get("target_id", "")
        conf = params.get("confidence", "?")
        if target:
            return f"Result recorded for {target}: {conf}", False
        summary = params.get("summary", "")
        label = summary[:80] if summary else conf
        return f"Result recorded: {label}", False

    def _execute_python(
        self, code: str, purpose: str = "", filename: str = ""
    ) -> tuple[str, bool]:
        """Write code to file, execute via sandbox, return (output, is_error)."""
        self._counter += 1
        self._computations_dir.mkdir(parents=True, exist_ok=True)

        # Build script name
        if filename:
            sanitized = self._sanitize_filename(filename)
            # Strip leading counter if agent already included one
            sanitized = re.sub(r"^\d+_", "", sanitized)
            if not sanitized or sanitized == ".py":
                sanitized = "script.py"
            script_name = f"{self._counter:03d}_{sanitized}"
        else:
            script_name = f"tool_exec_{self._counter:03d}.py"
        self._script_names.append(script_name)

        script_path = self._computations_dir / script_name
        script_path.write_text(code)

        result = execute_python(
            script_path,
            timeout=self.timeout,
            cwd=str(self._computations_dir),
            policy=self._policy,
        )

        # Determine exit status label
        if result.timed_out:
            exit_label = "timeout"
        elif result.returncode != 0:
            exit_label = f"error (rc={result.returncode})"
        else:
            exit_label = "success"

        # Build structured header
        header = f"=== {script_name} ===\n"
        if purpose:
            header += f"Purpose: {purpose}\n"
        header += f"Exit: {exit_label}\n\n"

        if result.timed_out:
            body = (
                f"TIMEOUT: Script exceeded {self.timeout}s limit.\n\n"
                "Suggestions:\n"
                "- Reduce grid/array sizes\n"
                "- Use fewer iterations or lower precision\n"
                "- Switch to analytical approaches where possible\n"
                "- Break the computation into smaller steps"
            )
            self._save_output_file(script_name, body)
            return header + body, True

        # Combine stdout/stderr for error cases
        if result.returncode != 0:
            raw_output = (
                result.stdout + "\n\nSTDERR:\n" + result.stderr
                if result.stdout
                else result.stderr
            )
        else:
            raw_output = result.stdout

        # Save full output before truncation
        self._save_output_file(script_name, raw_output)

        # Truncate the body portion only
        body = self._truncate_output(raw_output, self._output_limit)

        if result.returncode != 0:
            if "NameError" in result.stderr:
                body += (
                    "\n\n--- REMINDER ---\n"
                    "Each execute_python call runs in a FRESH Python process. No variables,\n"
                    "functions, or imports carry over from previous calls. You must include\n"
                    "ALL imports and function definitions in every script."
                )
            return header + body + self._progress_check_nudge(), True

        return header + body + self._progress_check_nudge(), False

    def _progress_check_nudge(self) -> str:
        """Arm the periodic progress check and return the model-facing prompt.

        ``progress_check_interval`` and ``report_progress`` shipped wired to
        nothing: no code ever set ``_progress_check_pending``, so the tool was
        never offered and the interval was inert. Every N scripts the executor
        now arms it, which puts ``report_progress`` into ``active_tools`` for
        the next round.
        """
        interval = self._progress_check_interval
        if interval <= 0 or self._counter % interval != 0:
            return ""
        self._progress_check_pending = True
        return (
            f"\n\n--- PROGRESS CHECK ---\n"
            f"You have run {self._counter} scripts. Call `report_progress` "
            "before your next execute_python call: summarize what the "
            "computations have shown, what you still need, and whether you "
            "are ready to conclude."
        )

    def _save_output_file(self, script_name: str, content: str) -> None:
        """Save script output to a companion .output file."""
        stem = Path(script_name).stem
        output_path = self._computations_dir / f"{stem}.output"
        output_path.write_text(content)

    @staticmethod
    def _truncate_output(text: str, limit: int = 10_000) -> str:
        """Truncate output to limit chars, preserving head and tail."""
        if len(text) <= limit:
            return text
        half = limit // 2
        return (
            text[:half]
            + f"\n\n[... truncated {len(text) - limit} chars ...]\n\n"
            + text[-half:]
        )
