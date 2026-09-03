"""Configuration for the physics mode."""

import json
from dataclasses import dataclass, field
from pathlib import Path
import os

import yaml


# ---------------------------------------------------------------------------
# Package defaults — single source of truth is config.default.yaml
# ---------------------------------------------------------------------------


def _load_package_defaults() -> dict:
    """Load defaults from the config.default.yaml shipped with the package."""
    path = Path(__file__).parent.parent / "config.default.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"Failed to load package defaults from {path}")
    return data


DEFAULTS = _load_package_defaults()


@dataclass
class Config:
    """Physics mode configuration.

    ``max_tokens`` is the maximum output-token budget per LLM call. When the
    model is registered in ``models.yaml`` its ``max_output_tokens`` field is
    authoritative; for unregistered models (the fork's normal case — models
    come from ``physics.model`` in config.yaml) it falls back to the package
    default from ``config.default.yaml``.
    """

    model: str = DEFAULTS["model"]
    max_tokens: int = 0  # resolved in __post_init__ (models.yaml or default)
    max_iterations: int = DEFAULTS["max_iterations"]
    api_retry_max: int = DEFAULTS["api_retry_max"]
    api_retry_initial_delay: float = DEFAULTS["api_retry_initial_delay"]
    api_retry_max_delay: float = DEFAULTS["api_retry_max_delay"]
    api_timeout: float = DEFAULTS["api_timeout"]
    parse_retries: int = DEFAULTS["parse_retries"]
    max_tokens_retries: int = DEFAULTS["max_tokens_retries"]
    agent_max_tokens: dict = field(
        default_factory=lambda: dict(DEFAULTS["agent_max_tokens"])
    )
    provider: str = ""
    # Model for whichever sub-agent is WRITING CODE. Empty means `model` does
    # everything. Resolved from physics.coder_model in config.yaml.
    coder_model: str = ""
    # Per-role overrides, from physics.agent_models. Beats coder_model and
    # model. Matched by exact agent name, then by longest prefix.
    agent_models: dict = field(default_factory=dict)
    # Sent as `reasoning_effort` when set. Vocabulary is the endpoint's:
    # low/medium/xhigh for Qwen3.8 on vLLM, low/medium/high for OpenAI.
    reasoning_effort: str = ""
    # Run the critic after every Nth iteration. 0 disables.
    critique_every_n: int = DEFAULTS["critique_every_n"]
    workspace_dir: str = ""
    logs_dir: str = ""
    api_key: str = ""
    model_id: str = ""  # Resolved API model ID (from models.yaml)
    input_cost: float = 0.0  # USD per million input tokens (from models.yaml)
    output_cost: float = 0.0  # USD per million output tokens (from models.yaml)
    reasoning: dict = field(default_factory=dict)  # provider-specific reasoning params

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

    def to_dict(self) -> dict:
        """Serialize config fields for persistence (excludes sensitive/derived fields)."""
        return {f: getattr(self, f) for f in _PERSIST_FIELDS}

    def save(self, workspace_root: Path) -> None:
        """Write config.json to the workspace root."""
        (workspace_root / "config.json").write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        )

    @classmethod
    def load(cls, workspace_root: Path, overrides: dict | None = None) -> "Config":
        """Load config from workspace config.json, merging optional overrides."""
        path = workspace_root / "config.json"
        if not path.exists():
            raise FileNotFoundError(f"No config.json found in {workspace_root}")
        data = json.loads(path.read_text())
        if overrides:
            # If user switches model, clear provider/model_id so __post_init__ re-resolves
            if "model" in overrides and overrides["model"] is not None:
                data.pop("provider", None)
                data.pop("model_id", None)
                data.pop("input_cost", None)
                data.pop("output_cost", None)
                data.pop("reasoning", None)
            for k, v in overrides.items():
                if v is not None:
                    data[k] = v
        # config.json files from before the research mode existed carry
        # pipeline-only fields; drop anything the dataclass no longer has.
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**data)

    def __post_init__(self):
        """Resolve what ``models.yaml`` can tell us; defer the rest to ``llm``.

        The fork ships no ``models.yaml`` — the endpoint, model name and API
        key all come from the son-of-anton ``config.yaml`` at call time (see
        ``physics_intern.llm._resolve_endpoint``). What survives here is the
        optional registry: when a ``models.yaml`` is present it still supplies
        the token budget and the per-million costs the cost report reads.
        """
        resolved = _resolve_model(self.model)
        # Resolve provider from models.yaml if not explicitly set.
        if not self.provider and resolved:
            self.provider = resolved["provider"]
            self.model_id = resolved["model_id"]
            self.input_cost = resolved.get("input_cost", 0.0)
            self.output_cost = resolved.get("output_cost", 0.0)
            self.reasoning = resolved.get("reasoning", {})
        # Resolve max_tokens from models.yaml — the single source of truth.
        # Runs on every init (including resume) so the value always reflects
        # the current registry even if the persisted config.json is stale.
        if resolved:
            model_max = resolved.get("max_output_tokens")
            if not model_max:
                raise ValueError(
                    f"models.yaml entry for {self.model!r} is missing the "
                    f"required 'max_output_tokens' field. Every model must "
                    f"declare its maximum output token budget."
                )
            self.max_tokens = int(model_max)
        elif not self.max_tokens:
            # Unregistered model (models.yaml absent/empty — the fork ships
            # without it): fall back to the package default rather than
            # refusing to run. The endpoint layer resolves the actual model
            # name and base URL from the son-of-anton config.yaml.
            self.max_tokens = int(DEFAULTS["max_tokens"])
        # If model_id wasn't resolved, fall back to model (direct API id)
        if not self.model_id:
            self.model_id = self.model
        # Resolve the API key only from the model registry, when there is one.
        if not self.api_key and resolved:
            self.api_key = os.environ.get(resolved["env_key"], "")


# Fields settable via config.yaml (workspace_dir, logs_dir, api_key excluded)
# max_tokens is intentionally excluded — it is derived from models.yaml only.
_YAML_CONFIG_FIELDS = frozenset(
    {
        "model",
        "max_iterations",
        "api_retry_max",
        "api_retry_initial_delay",
        "api_retry_max_delay",
        "api_timeout",
        "parse_retries",
        "max_tokens_retries",
        "agent_max_tokens",
        "provider",
        "coder_model",
        "agent_models",
        "reasoning_effort",
        "critique_every_n",
    }
)

# Fields persisted to config.json (superset of _YAML_CONFIG_FIELDS)
_PERSIST_FIELDS = _YAML_CONFIG_FIELDS | {
    "model_id",
    "input_cost",
    "output_cost",
    "reasoning",
}


def _resolve_model(model_key: str) -> dict | None:
    """Look up a model key in models.yaml, return {provider, model_id, env_key} or None."""
    path = Path(__file__).parent.parent / "models.yaml"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            registry = yaml.safe_load(f)
        if not registry or not isinstance(registry, dict):
            return None
        entry = registry.get(model_key)
        if not entry or not isinstance(entry, dict):
            return None
        reasoning = {}
        for key in (
            "reasoning_budget",
            "reasoning_effort",
            "thinking_level",
            "thinking",
            "effort",
            "reasoning_format",
            "hf_provider",
            "timeout",
            "base_url",
            "tool_mode",
        ):
            if key in entry:
                reasoning[key] = entry[key]
        result = {
            "provider": entry["provider"],
            "model_id": entry.get("model_id", model_key),
            "env_key": entry.get("env_key", "ANTHROPIC_API_KEY"),
            "input_cost": float(entry.get("input_cost", 0)),
            "output_cost": float(entry.get("output_cost", 0)),
            "reasoning": reasoning,
        }
        if "max_output_tokens" in entry:
            result["max_output_tokens"] = int(entry["max_output_tokens"])
        return result
    except (OSError, yaml.YAMLError):
        return None


def build_config(args: "object", overrides: dict | None = None) -> Config:
    """Build Config with 3-tier precedence: overrides > CLI args > config.yaml > defaults.

    *overrides* is a plain dict applied first (callers such as the
    Autophysicist runner use it for programmatic config tweaks).
    """
    kwargs: dict = {}

    # Layer 0: programmatic overrides
    if overrides:
        kwargs.update(overrides)

    # Layer 1: CLI args override (only non-None values).
    # max_tokens is intentionally absent — derived from models.yaml.
    cli_fields = {
        "model",
        "max_iterations",
        "workspace_dir",
    }
    for field_name in cli_fields:
        cli_name = field_name.replace("-", "_")
        value = getattr(args, cli_name, None) if args is not None else None
        if value is not None:
            kwargs[field_name] = value

    return Config(**kwargs)
