"""Tool executor for the autophysicist Research Manager."""

import time
from pathlib import Path
from typing import ClassVar

from ..core.config import Config
from ..state.tool_call import ToolCall
from ..utils.mcp import MCPToolset
from ..utils.sandbox import SandboxPolicy
from .memory import PermanentMemory, Scratchpad
from .subagent import dispatch_subagent


class ManagerToolExecutor:
    """Executes the Manager's tools and enforces the per-iteration budget.

    Conforms to the duck-type interface expected by ``run_agent_loop``:
    ``execute()``, ``active_tools``, ``stop_after_round``,
    ``exit_tool_name``, ``exit_tool_names``, ``end_round()``.

    Wind-down contract (enforced in :meth:`end_round`):

    * **Soft trigger** — fires once when either the running token total
      reaches ``token_budget`` *or* ``_tool_call_count`` reaches
      ``tool_call_cap``. Flips ``_wind_down`` so :attr:`active_tools`
      returns :attr:`WIND_DOWN_TOOLS` (``dispatch_subagent`` removed) and
      injects a user-visible warning. The Manager is expected to save
      state and call ``end_turn``.
    * **Hard trigger** — fires when the running total reaches
      ``HARD_BUDGET_MULTIPLIER × token_budget`` (default 1.5×). Sets
      ``stop_after_round = True`` so ``run_agent_loop`` exits even if the
      Manager has not called ``end_turn``; any unsaved state is lost.

    Token accounting: the Manager's own conversation tokens are supplied
    by ``run_agent_loop`` via the ``on_round`` callback (see
    :meth:`update_manager_tokens`). Sub-agent tokens are added here when
    ``dispatch_subagent`` returns. The wind-down total is the sum of both.
    """

    # ---- Tool definitions (OpenAI canonical format) ----

    _DISPATCH_SUBAGENT_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "Dispatch an ephemeral sub-agent to perform a specific task. "
                "The sub-agent receives only the system_prompt and user_message "
                "you provide — it has no access to the problem statement, memory, "
                "or prior context. If execute_code=true, Python code in the "
                "response will be extracted and executed in a sandbox (up to 3 "
                "retries on failure)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "system_prompt": {
                        "type": "string",
                        "description": (
                            "Complete system prompt for the sub-agent defining "
                            "its role, expertise, and expected output format."
                        ),
                    },
                    "user_message": {
                        "type": "string",
                        "description": "The specific task or question for the sub-agent.",
                    },
                    "execute_code": {
                        "type": "boolean",
                        "description": (
                            "If true, Python code from the response will be "
                            "extracted and executed in a sandbox. Default: false."
                        ),
                    },
                },
                "required": ["system_prompt", "user_message"],
            },
        },
    }

    _WRITE_PERMANENT_MEMORY_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "write_to_permanent_memory",
            "description": (
                "Append text to the permanent memory file. This content will be "
                "visible on every future iteration. Use ONLY for results that "
                "have been independently verified. Each entry is automatically "
                "tagged with the iteration number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The text to append. Should be self-contained: "
                            "include context, definitions, the result, and "
                            "how it was verified."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    }

    _WRITE_SCRATCHPAD_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "write_to_scratchpad",
            "description": (
                "Append working notes to the scratchpad. Only the last N entries "
                "(default: 5) are visible on the next iteration. Use for "
                "hypotheses, plans, intermediate results, and status updates. "
                "Important results must be promoted to permanent memory before "
                "they scroll off."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Working notes, hypotheses, plans, or status updates.",
                    },
                },
                "required": ["content"],
            },
        },
    }

    _END_TURN_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "end_turn",
            "description": (
                "Signal that you are done with this iteration. Your context will "
                "be erased and the next iteration will begin. Write to memory or "
                "scratchpad BEFORE calling this — anything not written down is lost."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }

    _SUBMIT_FINAL_ANSWER_DEF: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "submit_final_answer",
            "description": (
                "Submit the final answer to the problem and terminate the entire "
                "run. Use this ONLY when you are confident the problem is fully "
                "solved and the answer has been verified and written to permanent "
                "memory. This ends all iterations — there is no going back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "The complete final answer to the problem, including "
                            "the result, how it was derived, and how it was verified."
                        ),
                    },
                },
                "required": ["answer"],
            },
        },
    }

    # The Manager's own tools. MCP tools are added per instance (they are
    # discovered from an endpoint at run time, not known at import time), so
    # the tool sets the loop actually sees come from the methods below.
    ALL_TOOLS: ClassVar[list[dict]] = [
        _DISPATCH_SUBAGENT_DEF,
        _WRITE_PERMANENT_MEMORY_DEF,
        _WRITE_SCRATCHPAD_DEF,
        _END_TURN_DEF,
        _SUBMIT_FINAL_ANSWER_DEF,
    ]

    WIND_DOWN_TOOLS: ClassVar[list[dict]] = [
        _WRITE_PERMANENT_MEMORY_DEF,
        _WRITE_SCRATCHPAD_DEF,
        _END_TURN_DEF,
        _SUBMIT_FINAL_ANSWER_DEF,
    ]

    def all_tools(self) -> list[dict]:
        """Manager tools plus whatever MCP is configured and reachable."""
        return list(self.ALL_TOOLS) + self._mcp_tools()

    def wind_down_tools(self) -> list[dict]:
        """Wind-down drops dispatch_subagent AND the lookups.

        Wind-down means the iteration's budget is spent and the Manager should
        be writing down what it found. A literature search at that point starts
        work it has no budget to finish, and the result is lost with the
        context.
        """
        return list(self.WIND_DOWN_TOOLS)

    def _mcp_tools(self) -> list[dict]:
        return self.mcp.tools() if self.mcp else []

    HARD_BUDGET_MULTIPLIER = 1.5

    def __init__(
        self,
        config: Config,
        permanent_memory: PermanentMemory,
        scratchpad: Scratchpad,
        workspace_root: Path,
        iteration: int,
        token_budget: int = 64_000,
        tool_call_cap: int = 15,
        sandbox_timeout: int = 60,
        policy: SandboxPolicy | None = None,
        coder_model: str = "",
        mcp: MCPToolset | None = None,
    ):
        self.config = config
        self.permanent_memory = permanent_memory
        self.scratchpad = scratchpad
        self.workspace_root = workspace_root
        self.iteration = iteration
        self.token_budget = token_budget
        self.tool_call_cap = tool_call_cap
        self.sandbox_timeout = sandbox_timeout
        self.coder_model = coder_model
        self.mcp = mcp
        self.policy = policy or SandboxPolicy.from_config()
        if self.policy.workspace is None:
            self.policy.workspace = Path(workspace_root)

        # Duck-type protocol state for run_agent_loop
        self.stop_after_round = False
        self.problem_solved = False
        self.final_answer = ""
        self._wind_down = False
        self._tool_call_count = 0
        self._subagent_counter = 0

        # Token tracking — updated by on_round callback from runner
        self._total_manager_tokens: int = 0
        self.subagent_input_tokens: int = 0
        self.subagent_output_tokens: int = 0

    @property
    def exit_tool_name(self) -> str:
        return "end_turn"

    @property
    def exit_tool_names(self) -> frozenset[str]:
        return frozenset({"end_turn", "submit_final_answer"})

    @property
    def active_tools(self) -> list[dict] | None:
        """Dynamic tool switching for wind-down phase."""
        if self._wind_down:
            return self.wind_down_tools()
        return self.all_tools()

    def update_manager_tokens(self, total_input: int, total_output: int) -> None:
        """Called by on_round callback to track manager conversation tokens."""
        self._total_manager_tokens = total_input + total_output

    def end_round(self) -> str | None:
        """Called by run_agent_loop after each round's tool results.

        Returns a string to inject as user message, or None.
        Handles budget and tool-call-cap wind-down transitions.
        """
        total = (
            self._total_manager_tokens
            + self.subagent_input_tokens
            + self.subagent_output_tokens
        )

        # Hard cap — force immediate termination
        if total >= self.token_budget * self.HARD_BUDGET_MULTIPLIER:
            self.stop_after_round = True
            return (
                f"HARD BUDGET LIMIT: {total:,} tokens used "
                f"(hard cap: {int(self.token_budget * self.HARD_BUDGET_MULTIPLIER):,}). "
                "Iteration is being terminated. Any unsaved results are lost."
            )

        # Wind-down from token budget
        if not self._wind_down and total >= self.token_budget:
            self._wind_down = True
            return (
                "CONTEXT BUDGET WARNING: You have used approximately "
                f"{total:,} tokens this iteration "
                f"(budget: {self.token_budget:,}). "
                "Dispatching sub-agents is no longer available. "
                "Please write any important results to permanent memory "
                "or scratchpad, then call end_turn()."
            )

        # Wind-down from tool call cap
        if not self._wind_down and self._tool_call_count >= self.tool_call_cap:
            self._wind_down = True
            return (
                f"TOOL CALL CAP REACHED: {self._tool_call_count} tool calls "
                f"(cap: {self.tool_call_cap}). "
                "Dispatching sub-agents is no longer available. "
                "Write results to memory/scratchpad and call end_turn()."
            )

        return None

    def execute(self, tool_name: str, tool_input: dict) -> ToolCall:
        """Dispatch a tool call by name."""
        start = time.time()
        self._tool_call_count += 1

        if tool_name == "dispatch_subagent":
            output, is_error = self._dispatch_subagent(tool_input)
        elif tool_name == "write_to_permanent_memory":
            output, is_error = self._write_permanent_memory(tool_input)
        elif tool_name == "write_to_scratchpad":
            output, is_error = self._write_scratchpad(tool_input)
        elif tool_name == "end_turn":
            output, is_error = self._end_turn()
        elif tool_name == "submit_final_answer":
            output, is_error = self._submit_final_answer(tool_input)
        elif self.mcp is not None and self.mcp.handles(tool_name):
            if self._wind_down:
                output, is_error = (
                    f"ERROR: {tool_name} is unavailable during wind-down. Write "
                    "your results to memory/scratchpad and call end_turn()."
                ), True
            else:
                output, is_error = self.mcp.call(tool_name, tool_input)
        else:
            available = [
                "dispatch_subagent",
                "write_to_permanent_memory",
                "write_to_scratchpad",
                "end_turn",
                "submit_final_answer",
            ] + [t["function"]["name"] for t in self._mcp_tools()]
            output = (
                f"ERROR: Unknown tool '{tool_name}'. Available: "
                + ", ".join(available)
                + "."
            )
            is_error = True

        return ToolCall(
            tool_name=tool_name,
            tool_input=tool_input,
            output=output,
            is_error=is_error,
            duration=time.time() - start,
        )

    def _dispatch_subagent(self, params: dict) -> tuple[str, bool]:
        if self._wind_down:
            return (
                "ERROR: dispatch_subagent is unavailable during wind-down phase. "
                "Write your results to memory/scratchpad and call end_turn()."
            ), True

        system_prompt = params.get("system_prompt", "")
        user_message = params.get("user_message", "")
        execute_code = params.get("execute_code", False)

        if not system_prompt or not user_message:
            return "ERROR: Both system_prompt and user_message are required.", True

        self._subagent_counter += 1
        result = dispatch_subagent(
            system_prompt=system_prompt,
            user_message=user_message,
            execute_code=execute_code,
            config=self.config,
            workspace_root=self.workspace_root,
            iteration=self.iteration,
            subagent_counter=self._subagent_counter,
            sandbox_timeout=self.sandbox_timeout,
            policy=self.policy,
            model=self.coder_model,
        )

        self.subagent_input_tokens += result.total_input_tokens
        self.subagent_output_tokens += result.total_output_tokens

        return result.format_for_manager(), False

    def _write_permanent_memory(self, params: dict) -> tuple[str, bool]:
        content = params.get("content", "")
        if not content.strip():
            return "ERROR: content cannot be empty.", True
        return self.permanent_memory.append(content, self.iteration), False

    def _write_scratchpad(self, params: dict) -> tuple[str, bool]:
        content = params.get("content", "")
        if not content.strip():
            return "ERROR: content cannot be empty.", True
        return self.scratchpad.append(content, self.iteration), False

    def _end_turn(self) -> tuple[str, bool]:
        self.stop_after_round = True
        return "Turn ended. Context will be erased.", False

    def _submit_final_answer(self, params: dict) -> tuple[str, bool]:
        answer = params.get("answer", "")
        if not answer.strip():
            return "ERROR: answer cannot be empty.", True
        self.stop_after_round = True
        self.problem_solved = True
        self.final_answer = answer
        return "Final answer submitted. Run will terminate.", False
