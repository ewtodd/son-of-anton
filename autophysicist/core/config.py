"""Configuration for the physics mode."""

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def agent_config() -> dict:
    """The son-of-anton config.toml, or ``{}`` when it cannot be loaded."""
    try:
        from son_of_anton_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def physics_config() -> dict:
    """The ``physics`` section of config.toml, or ``{}``."""
    section = agent_config().get("physics")
    return section if isinstance(section, dict) else {}


def _load_package_defaults() -> dict:
    """Load defaults from the config.default.toml shipped with the package."""
    path = Path(__file__).parent.parent / "config.default.toml"
    with open(path, encoding="utf-8") as f:
        data = tomllib.loads(f.read())
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"Failed to load package defaults from {path}")
    return data


DEFAULTS = _load_package_defaults()


@dataclass
class Config:
    """Physics mode configuration.

    ``max_tokens`` is the maximum output-token budget per LLM call, from
    ``config.default.toml``; ``agent_max_tokens`` overrides it per agent. The
    endpoint, model name and API key come from the son-of-anton
    ``config.toml`` at call time (see ``autophysicist.llm._resolve_endpoint``).
    """

    model: str = DEFAULTS["model"]
    max_tokens: int = int(DEFAULTS["max_tokens"])
    max_iterations: int = DEFAULTS["max_iterations"]
    api_retry_max: int = DEFAULTS["api_retry_max"]
    api_retry_initial_delay: float = DEFAULTS["api_retry_initial_delay"]
    api_retry_max_delay: float = DEFAULTS["api_retry_max_delay"]
    api_retry_total_timeout: float = DEFAULTS["api_retry_total_timeout"]
    api_timeout: float = DEFAULTS["api_timeout"]
    max_tokens_retries: int = DEFAULTS["max_tokens_retries"]
    agent_max_tokens: dict = field(
        default_factory=lambda: dict(DEFAULTS["agent_max_tokens"])
    )
    # Model for whichever sub-agent is WRITING CODE. Empty means `model` does
    # everything. Resolved from physics.coder_model in config.toml.
    coder_model: str = ""
    # Per-role overrides, from physics.agent_models. Beats coder_model and
    # model. Matched by exact agent name, then by longest prefix.
    agent_models: dict = field(default_factory=dict)
    # Sent as `reasoning_effort` when set. Vocabulary is the endpoint's:
    # low/medium/xhigh for Qwen3.8 on vLLM, low/medium/high for OpenAI.
    reasoning_effort: str = ""
    # The model's context window in tokens, from physics.context_length. 0
    # resolves it from config and endpoint metadata at run time
    # (autophysicist.llm.resolve_context_window); an overflow error that names
    # the real window overwrites it.
    context_length: int = 0
    # Run the critic after every Nth iteration. 0 disables.
    critique_every_n: int = DEFAULTS["critique_every_n"]
    # Who writes the critique: "model" (default) or "human". Human mode is
    # attended: the run renders the iteration's activity and diff, opens
    # $EDITOR, and takes a free-text critique plus a structured verdict.
    # From physics.critic.mode in config.toml.
    critic_mode: str = "model"
    workspace_dir: str = ""

    def max_tokens_for_agent(self, agent_name: str) -> int:
        """Return the max output tokens for a specific agent.

        Matches ``agent_max_tokens`` by exact name first, then by longest
        prefix — the sub-agents are named per dispatch
        ("subagent_iter3_2"), so an exact-match-only lookup could never give
        them anything but the model-level default.
        """
        if agent_name in self.agent_max_tokens:
            return self.agent_max_tokens[agent_name]
        matches = [
            key for key in self.agent_max_tokens if agent_name.startswith(key)
        ]
        if matches:
            return self.agent_max_tokens[max(matches, key=len)]
        return self.max_tokens

    def model_for_agent(self, agent_name: str, *, coding: bool = False) -> str:
        """Which model *agent_name* runs under.

        Three tiers, most specific first: an explicit ``physics.agent_models``
        entry, then ``coder_model`` when the call is writing code, then
        ``model``.

        The middle tier exists because "is this call writing code" is not the
        same question as "which agent is this". A sub-agent dispatched with
        execute_code is doing the coding job whatever it is called, while the
        same sub-agent's non-coding turns are not.
        """
        if agent_name in self.agent_models:
            return str(self.agent_models[agent_name])
        matches = [k for k in self.agent_models if agent_name.startswith(k)]
        if matches:
            return str(self.agent_models[max(matches, key=len)])
        if coding and self.coder_model:
            return self.coder_model
        return self.model

    def save(self, workspace_root: Path) -> None:
        """Write the resolved config to the workspace root as config.json."""
        data = {f: getattr(self, f) for f in _PERSIST_FIELDS}
        (workspace_root / "config.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )


# Fields written to config.json (workspace_dir excluded; max_tokens is a
# package default, not a setting).
_PERSIST_FIELDS = frozenset(
    {
        "model",
        "max_iterations",
        "api_retry_max",
        "api_retry_initial_delay",
        "api_retry_max_delay",
        "api_retry_total_timeout",
        "api_timeout",
        "max_tokens_retries",
        "agent_max_tokens",
        "coder_model",
        "agent_models",
        "reasoning_effort",
        "context_length",
        "critique_every_n",
        "critic_mode",
    }
)
