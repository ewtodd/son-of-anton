"""Which model each physics role runs under.

Two roles, not two modes. The reasoning role — the Autophysicist's Research
Manager, the research pipeline's other agents — reads state, decides strategy
and judges results. The coding role — the Autophysicist's ``execute_code``
sub-agents, the pipeline's computer agent — writes one self-contained script.

A deployment may have a model that is much faster and better at the second job
and weaker at the first, so both modes read the same two keys and apply them to
the same two roles.
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
