"""Sub-agent dispatch with optional code execution and retry."""

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.config import Config
from ..llm import call_llm, run_agent_loop
from ..utils.mcp import LookupExecutor, MCPToolset
from ..utils.sandbox import (
    SandboxPolicy,
    describe_runtime,
    execute_python,
    runtime_guidance,
)


def code_execution_suffix(
    policy: SandboxPolicy | None = None, timeout: int = 60
) -> str:
    """Build the code-execution rules from the runtime the code will run in.

    The package list is probed, not asserted: the previous hardcoded
    "NumPy, SciPy, SymPy, matplotlib" was a promise the agent's own venv could
    not keep, and a sub-agent that believes it has NumPy writes NumPy.
    """
    interpreter = policy.interpreter if policy else None
    guidance = runtime_guidance(interpreter, timeout=timeout)
    guidance = ("\n\n" + guidance) if guidance else ""
    data_note = ""
    if policy and policy.data_dirs:
        listed = "\n".join(f"   - {d}" for d in policy.data_dirs)
        data_note = (
            "\n8. Read-only data is mounted at these paths — read from them "
            f"directly, do not copy them:\n{listed}"
        )
    return f"""

## Code execution instructions

You will write Python code to perform the requested computation. Follow these rules:

1. Write your reasoning and explanation as normal text.
2. Write exactly ONE Python code block using triple backticks with the `python` language tag.
3. The script must be completely self-contained: include all imports and definitions.
4. Print all results to stdout. The printed output is what will be returned.
5. Available: {describe_runtime(interpreter)}
6. Do NOT call plt.show() — use plt.savefig() then plt.close().
7. Timeout: {timeout} seconds. The script runs sandboxed with no network access
   and no access to the home directory; the working directory is writable and
   persists, everything else is discarded.{data_note}{guidance}
"""


@dataclass
class SubAgentResult:
    """Result from a sub-agent dispatch."""

    reasoning_text: str
    code: str
    execution_output: str
    execution_status: str  # "success", "failed_after_retries", "no_code"
    total_input_tokens: int
    total_output_tokens: int

    def format_for_manager(self) -> str:
        """Format as a string to return to the Manager."""
        parts = [f"<subagent_reasoning>\n{self.reasoning_text}\n</subagent_reasoning>"]
        if self.code:
            parts.append(f"\n\n<code>\n{self.code}\n</code>")
            parts.append(
                f'\n\n<execution_output status="{self.execution_status}">\n'
                f"{self.execution_output}\n</execution_output>"
            )
        return "\n".join(parts)


def _extract_python_code(text: str) -> str:
    """Extract the first Python code block from text."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _save_output(script_path: Path, result) -> None:
    """Write a script's stdout/stderr next to it as <stem>.output."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"\n--- STDERR ---\n{result.stderr}")
    try:
        script_path.with_suffix(".output").write_text(
            "".join(parts) or "(no output)", encoding="utf-8"
        )
    except OSError:
        pass


def _error_headline(result) -> str:
    """The last non-empty stderr line — the exception, not the traceback.

    A retry that gets 5000 characters of truncated traceback tends to fix
    whatever it notices first. Leading with `AttributeError: 'TTree' object has
    no attribute 'SetMaxTreeError'` puts the actual cause where it cannot be
    skimmed past; the run this came from repaired an unrelated syntax error
    instead and kept the bad call.
    """
    for line in reversed((result.stderr or "").strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _truncate(text: str, limit: int = 10_000) -> str:
    """Truncate output, preserving head and tail."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + f"\n\n[... truncated {len(text) - limit} chars ...]\n\n"
        + text[-half:]
    )


def dispatch_subagent(
    system_prompt: str,
    user_message: str,
    execute_code: bool,
    config: Config,
    workspace_root: Path,
    iteration: int,
    subagent_counter: int = 0,
    sandbox_timeout: int = 60,
    max_retries: int = 3,
    policy: SandboxPolicy | None = None,
    model: str = "",
    mcp: MCPToolset | None = None,
) -> SubAgentResult:
    """Dispatch an ephemeral sub-agent LLM call.

    If *execute_code* is True, extracts Python code from the response,
    executes in a sandbox, and retries up to *max_retries* times on failure.

    *model* overrides the Manager's model, but only when *execute_code* is set.
    That is the whole distinction worth drawing: a sub-agent asked to write a
    self-contained fitting script is doing a coding job, and a deployment may
    have a model that is much faster and better at exactly that. A sub-agent
    asked to derive a result, check a derivation for a dropped factor, or argue
    the other side is doing the same physics reasoning the Manager does, and
    routing it to a coding model to save latency trades away the thing the
    sub-agent was dispatched for.

    Code-writing dispatches are also where the volume is — one per script, plus
    up to three more each time a script fails — so this is where a faster model
    is worth the most and costs the least.
    """
    total_in = 0
    total_out = 0

    if model and execute_code and model != config.model:
        config = copy.copy(config)
        config.model = model

    if policy is None:
        policy = SandboxPolicy.from_config()
    if policy.workspace is None:
        policy.workspace = Path(workspace_root)

    effective_system = system_prompt
    if execute_code:
        effective_system = system_prompt + code_execution_suffix(
            policy, sandbox_timeout
        )

    agent_label = f"subagent_iter{iteration}_{subagent_counter}"
    lookups = mcp.tools_for("subagent") if mcp else []

    def _ask(user_content: str, label: str):
        """One sub-agent turn, with documentation lookups if it has any.

        A sub-agent used to be a pure function of its prompt: no memory, no
        tools, nothing but what the Manager wrote. That is a real anti-drift
        property and it costs nothing for a derivation, where there is nothing
        to look up. It is the wrong trade for writing code against a library
        the model has not memorised — that failure mode is guessing at an API,
        and it is answered by reading the API.

        The tool loop is lookups ONLY. There is no exit tool: the turn ends
        when the sub-agent stops calling tools and answers, which is exactly
        what it did before, so the "one Python code block" contract is
        unchanged.
        """
        if not lookups:
            response = call_llm(
                system=effective_system,
                user_content=user_content,
                config=config,
                agent_name=label,
                iteration=iteration,
            )
            return response.text, response.input_tokens, response.output_tokens
        result = run_agent_loop(
            system=effective_system,
            user_content=user_content,
            config=config,
            tool_executor=LookupExecutor(mcp, "subagent"),
            tools=lookups,
            max_rounds=6,
            agent_name=label,
            iteration=iteration,
        )
        return result.text, result.total_input_tokens, result.total_output_tokens

    text, used_in, used_out = _ask(user_message, agent_label)
    total_in += used_in
    total_out += used_out

    if not execute_code:
        return SubAgentResult(
            reasoning_text=text,
            code="",
            execution_output="",
            execution_status="no_code",
            total_input_tokens=total_in,
            total_output_tokens=total_out,
        )

    # --- Code execution path ---
    code = _extract_python_code(text)
    # Strip the code block from reasoning to avoid duplication in format_for_manager
    if code:
        reasoning_text = re.sub(
            r"```(?:python)?\s*\n.*?```",
            "",
            text,
            count=1,
            flags=re.DOTALL,
        ).strip()
    else:
        reasoning_text = text

    if not code:
        return SubAgentResult(
            reasoning_text=reasoning_text,
            code="",
            execution_output="No Python code block found in sub-agent response.",
            execution_status="no_code",
            total_input_tokens=total_in,
            total_output_tokens=total_out,
        )

    computations_dir = workspace_root / "computations"
    computations_dir.mkdir(parents=True, exist_ok=True)

    last_error = ""

    for attempt in range(1, max_retries + 1):
        script_name = f"{agent_label}_attempt{attempt}.py"
        script_path = computations_dir / script_name
        script_path.write_text(code)

        result = execute_python(
            script_path,
            timeout=sandbox_timeout,
            # Run at the workspace root: problem specs instruct the model to
            # write artifacts (decay.csv, RESULTS.txt) relative to the
            # workspace, and the formal evaluator reads them there.
            cwd=str(workspace_root),
            policy=policy,
        )
        # Keep what the script printed. Only the truncated head and tail reach
        # the Manager, and nothing else records it at all — so a run's actual
        # numeric output was unrecoverable afterwards, by the Manager or by a
        # human reading the workspace.
        _save_output(script_path, result)

        if result.returncode == 0 and not result.timed_out:
            output = result.stdout
            if result.stderr:
                output += f"\n\nSTDERR (warnings):\n{result.stderr}"
            return SubAgentResult(
                reasoning_text=reasoning_text,
                code=code,
                execution_output=_truncate(output),
                execution_status="success",
                total_input_tokens=total_in,
                total_output_tokens=total_out,
            )

        # Build error description
        if result.timed_out:
            last_error = f"TIMEOUT: Script exceeded {sandbox_timeout}s limit."
        else:
            last_error = result.stderr or f"Exit code {result.returncode}"
            if result.stdout:
                last_error = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{last_error}"
        last_error = _truncate(last_error, 5_000)

        if attempt < max_retries:
            headline = _error_headline(result)
            # A missing module is not a bug in the script, it is a wrong
            # premise about the environment — and repeating "fix the error"
            # gets the same import back. Say what is actually installed.
            available = ""
            if "ModuleNotFoundError" in headline or "ImportError" in headline:
                from ..utils.sandbox import describe_runtime

                available = (
                    "\n\nTHAT PACKAGE IS NOT INSTALLED AND WILL NOT BECOME "
                    "INSTALLED. Do not import it again, and do not try to "
                    "install it — there is no network. Available: "
                    f"{describe_runtime(policy.interpreter if policy else None)}\n"
                    "If your instructions named that package, they were wrong; "
                    "use what is available instead."
                )
            retry_msg = (
                f"Your code failed (attempt {attempt}/{max_retries}).\n\n"
                + (f"WHAT WENT WRONG: {headline}\n" if headline else "")
                + available
                + "\n"
                + f"Full output:\n```\n{last_error}\n```\n\n"
                + "Fix THAT specific cause — do not rewrite parts that were "
                "working, and do not assume an API exists because it sounds "
                "plausible. Write the complete corrected script in a single "
                "Python code block."
            )
            # The retry gets lookups too — this is precisely the moment the
            # docs are worth reading, since the failure is usually an API that
            # does not exist.
            retry_content = (
                f"{user_message}\n\n---\n\nYou already tried this:\n\n"
                f"```python\n{code}\n```\n\n{retry_msg}"
            )
            text, used_in, used_out = _ask(
                retry_content, f"{agent_label}_retry{attempt}"
            )
            total_in += used_in
            total_out += used_out
            reasoning_text = text
            new_code = _extract_python_code(text)
            if new_code:
                code = new_code

    # All retries exhausted
    return SubAgentResult(
        reasoning_text=reasoning_text,
        code=code,
        execution_output=f"{last_error}\n\nExecution failed after {max_retries} attempts.",
        execution_status="failed_after_retries",
        total_input_tokens=total_in,
        total_output_tokens=total_out,
    )
