"""Which model each physics role runs under.

Two roles to start with, not two modes. The reasoning role — the
Autophysicist's Research Manager, the research pipeline's judging agents —
reads state, decides strategy and judges results. The coding role — the
Autophysicist's ``execute_code`` sub-agents, the pipeline's computer — writes
one self-contained script. A deployment may have a model much better and faster
at the second and weaker at the first, so ``physics.model`` and
``physics.coder_model`` apply to those two roles in both modes.

``physics.agent_models`` goes finer, per agent name, and beats both. The
pipeline has nine roles and they are not all the same job: a formatter
rendering an answer template and a critic hunting for a dropped factor of two
want different things, and on a host serving one thinking model and one
instruct profile of the same weights, that distinction is free.
"""

from __future__ import annotations


def _physics_config() -> dict:
    try:
        from son_of_anton_cli.config import load_config

        section = (load_config() or {}).get("physics")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def resolve_models(config, model: str | None = None) -> None:
    """Fill ``config.model`` and ``config.coder_model`` from config.yaml.

    An explicit *model* wins; otherwise ``physics.model`` bridges in so the
    Config carries the real model name for logging, while the endpoint layer
    keeps its own base_url/model resolution order.
    """
    physics = _physics_config()
    if model:
        config.model = model
    elif not config.model:
        config.model = str(physics.get("model") or "").strip()
    if not config.coder_model:
        config.coder_model = str(physics.get("coder_model") or "").strip()
    if not config.reasoning_effort:
        config.reasoning_effort = str(
            physics.get("reasoning_effort") or ""
        ).strip()
    declared_critique = physics.get("critique_every_n")
    if declared_critique is not None:
        try:
            config.critique_every_n = max(int(declared_critique), 0)
        except (TypeError, ValueError):
            pass
    if not config.agent_models:
        declared = physics.get("agent_models")
        if isinstance(declared, dict):
            config.agent_models = {
                str(k): str(v) for k, v in declared.items() if str(v).strip()
            }
