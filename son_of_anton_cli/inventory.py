"""Config snapshot and provider rows for the /model and auxiliary-task pickers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class ConfigContext:
    """The model + provider config every picker needs."""

    current_provider: str
    current_model: str
    current_base_url: str
    user_providers: dict
    custom_providers: list
    excluded_providers: list = None

    def with_overrides(
        self,
        *,
        current_provider: Optional[str] = None,
        current_model: Optional[str] = None,
        current_base_url: Optional[str] = None,
    ) -> "ConfigContext":
        """A copy with truthy overrides (live session state) applied."""
        kw = {k: v for k, v in (
            ("current_provider", current_provider),
            ("current_model", current_model),
            ("current_base_url", current_base_url),
        ) if v}
        return replace(self, **kw) if kw else self


def load_picker_context() -> ConfigContext:
    from son_of_anton_cli.config import get_compatible_custom_providers, load_config

    cfg = load_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        current_model = model_cfg.get("default", model_cfg.get("name", "")) or ""
        current_provider = model_cfg.get("provider", "") or ""
        current_base_url = model_cfg.get("base_url", "") or ""
    else:
        current_model, current_provider, current_base_url = str(model_cfg or ""), "", ""
    return ConfigContext(
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
        user_providers={},
        custom_providers=get_compatible_custom_providers(cfg),
        excluded_providers=[],
    )


def build_models_payload(ctx: ConfigContext, **kwargs) -> dict:
    """``{providers, model, provider}`` for the pickers (rows from
    :func:`son_of_anton_cli.model_switch.list_authenticated_providers`)."""
    from son_of_anton_cli.model_switch import list_authenticated_providers

    allowed = {"max_models", "refresh", "probe_custom_providers",
               "probe_current_custom_provider", "show_all_models", "for_picker"}
    rows = list_authenticated_providers(
        current_provider=ctx.current_provider,
        current_base_url=ctx.current_base_url,
        current_model=ctx.current_model,
        custom_providers=ctx.custom_providers,
        **{k: v for k, v in kwargs.items() if k in allowed},
    )
    return {"providers": rows, "model": ctx.current_model, "provider": ctx.current_provider}


def build_aux_picker_rows(
    *,
    current_provider: str = "",
    current_model: str = "",
    current_base_url: str = "",
    max_models: int | None = None,
) -> list[dict]:
    """Provider rows for an auxiliary-task picker (vision, compaction, …)."""
    ctx = load_picker_context().with_overrides(
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
    )
    return build_models_payload(
        ctx, for_picker=True, probe_custom_providers=False,
        probe_current_custom_provider=True, max_models=max_models,
    )["providers"]


def format_aux_picker_entries(
    rows: list[dict],
    *,
    current_provider: str = "",
    current_base_url: str = "",
) -> list[tuple[str, str, list[str]]]:
    """Aux-picker rows as ``(slug, label, models)`` menu entries."""
    entries: list[tuple[str, str, list[str]]] = []
    current_slug = str(current_provider or "").strip().lower()
    has_base_url = bool(str(current_base_url or "").strip())
    for row in rows:
        slug = str(row.get("slug") or "")
        total = row.get("total_models") or len(row.get("models") or [])
        model_hint = f" — {total} models" if total else ""
        marker = "  ← current" if current_slug and slug.lower() == current_slug and not has_base_url else ""
        entries.append((slug, f"{row.get('name') or slug}{model_hint}{marker}", list(row.get("models") or [])))
    return entries
