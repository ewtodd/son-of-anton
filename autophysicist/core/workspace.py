"""Workspace resolution and event logging for physics runs."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def resolve_workspace_root(problem_name: str, model: str, kind: str) -> Path:
    """Return a fresh, ABSOLUTE workspace root for one physics run.

    Never relative. A relative root resolves against the process cwd, which
    for the gateway is the profile's home directory and for the CLI is the
    user's project — and the run then runs ``git init``, ``git add -A`` and
    ``git commit`` inside it. A run must never be able to touch a directory
    it did not create.

    Base directory: ``physics.workspace_root`` from config.toml when set,
    otherwise ``~/.son-of-anton/workspaces``.
    """
    from son_of_anton_constants import get_son_of_anton_home

    from .config import physics_config

    base = str(physics_config().get("workspace_root") or "").strip()
    if base:
        base_dir = Path(os.path.expanduser(base))
    else:
        base_dir = Path(get_son_of_anton_home()) / "workspaces"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_problem = (problem_name or "session").replace("/", "-")[:60]
    safe_model = (model or "default").replace("/", "-").replace(":", "-")
    root = base_dir / f"{timestamp}_{safe_problem}_{safe_model}_{kind}"

    # Collision guard: two runs inside the same second must not share a root.
    suffix = 1
    candidate = root
    while candidate.exists():
        suffix += 1
        candidate = Path(f"{root}-{suffix}")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate.resolve()


def assert_safe_workspace_root(root: Path, *, must_be_fresh: bool = True) -> None:
    """Refuse to initialize a workspace over a directory we do not own.

    A fresh run runs ``git init`` + ``git add -A`` + ``git commit`` in its
    workspace root. Pointed at a real directory that is a no-op at best and
    at worst commits the user's entire home (SSH keys included) into a new
    repository. A fresh workspace root must therefore be absolute and must
    not be an existing git repository or a home directory.

    ``must_be_fresh=False`` is for resuming an existing workspace, where the
    ``.git`` directory IS the workspace; the home/root refusal still applies.
    """
    if not root.is_absolute():
        raise ValueError(
            f"Refusing to initialize a physics workspace at a relative path "
            f"({root!r}). It would resolve against the process working "
            f"directory. Pass an absolute workspace root — see "
            f"autophysicist.core.workspace.resolve_workspace_root()."
        )
    resolved = root.resolve()
    if must_be_fresh and (resolved / ".git").exists():
        raise ValueError(
            f"Refusing to initialize a physics workspace inside an existing "
            f"git repository ({resolved}). 'git add -A' there would commit "
            f"unrelated files."
        )
    for reserved in {Path.home().resolve(), Path("/")}:
        if resolved == reserved:
            raise ValueError(
                f"Refusing to initialize a physics workspace directly in "
                f"{resolved}."
            )


def log_scaffold_event(
    workspace_dir: str | Path,
    iteration: int,
    category: str,
    event: str,
    detail: str = "",
) -> None:
    """Append one scaffold event to EVENT_LOG.jsonl. Never raises."""
    try:
        entry = {
            "kind": "scaffold",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "iter": iteration,
            "category": category,
            "event": event,
            "detail": detail,
        }
        with open(Path(workspace_dir) / "EVENT_LOG.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_llm_call(
    workspace_dir: str | Path,
    agent: str,
    iteration: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str,
    duration_s: float,
    round: int = 0,
) -> None:
    """Append one LLM-call event to EVENT_LOG.jsonl. Never raises.

    The ``*_chars`` and reasoning/answer token fields are never measured and
    always 0; they stay so every record keeps the shape readers expect.
    """
    try:
        entry = {
            "kind": "llm_call",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent": agent,
            "iter": iteration,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "stop_reason": stop_reason,
            "duration_s": duration_s,
            "system_prompt_chars": 0,
            "user_content_chars": 0,
            "response_chars": 0,
            "reasoning_tokens": 0,
            "answer_tokens": 0,
            "round": round,
        }
        with open(Path(workspace_dir) / "EVENT_LOG.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
