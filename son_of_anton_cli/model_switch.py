"""Shared /model logic for the CLI/TUI and the gateway.

Flag parsing, the picker's provider rows (one per ``custom_providers`` entry,
listing the endpoint's live ``/models`` catalog minus what it marks
unavailable, or a declared subset), and the switch itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def _declared_model_ids(value: Any) -> list[str]:
    """Model ids from a ``models`` value: mapping keys, list of ids or
    ``{"id"/"name": ...}`` dicts, or a single string."""
    ids: list[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate.strip() and candidate.strip().lower() not in seen:
            seen.add(candidate.strip().lower())
            ids.append(candidate.strip())

    if isinstance(value, str):
        _add(value)
    elif isinstance(value, dict):
        for model_id in value:
            if model_id not in {"__explicit_model_allowlist__", "__discovered_model_catalog__"}:
                _add(model_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _add(item if isinstance(item, str) else (item.get("id") or item.get("name")) if isinstance(item, dict) else None)
    return ids


def _entry_models_discovered(entry: Any) -> bool:
    """True when the entry's ``models`` were written by discovery, not the user."""
    if not isinstance(entry, dict):
        return False
    models = entry.get("models")
    return entry.get("models_discovered") is True or (
        isinstance(models, dict) and models.get("__discovered_model_catalog__") is True
    )


def _entry_catalog_pinned(entry: Any) -> bool:
    """True when a custom-provider entry declares its own model subset.

    A user-authored ``models`` value narrows the picker to that subset
    unless ``discover_models = true`` (then it is per-model metadata only).
    """
    if not isinstance(entry, dict) or _entry_models_discovered(entry) or entry.get("discover_models") is True:
        return False
    return bool(_declared_model_ids(entry.get("models")))


def _entry_slug(entry: dict) -> str:
    identity = str(entry.get("provider_key") or "").strip() or str(entry.get("name") or "").strip()
    normalized = identity.lower().replace(" ", "-")
    return normalized if normalized.startswith("custom:") else f"custom:{normalized}"


def _entry_aliases(entry: dict) -> set:
    aliases: set = set()
    for value in (entry.get("name"), entry.get("provider_key")):
        raw = str(value or "").strip().lower()
        if raw:
            normalized = raw.replace(" ", "-")
            aliases.update({raw, normalized, f"custom:{normalized}"})
    return aliases


def _scoped_key_env(name: str) -> str:
    """A ``key_env`` credential, read from .env first like every other credential."""
    if not name:
        return ""
    try:
        from son_of_anton_cli.config import get_env_value

        return (get_env_value(name) or "").strip()
    except Exception:
        return ""


def _fetch_picker_live_models(
    api_key: str,
    api_url: str,
    *_unused: Any,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    api_mode: str | None = None,
) -> list[str] | None:
    """The endpoint's catalog through the disk cache, or None."""
    from son_of_anton_cli.models import cached_fetch_api_models

    models = cached_fetch_api_models(api_key, api_url, timeout=timeout, headers=headers, api_mode=api_mode)
    return models or None


def _drop_unavailable(
    models: list[str] | None,
    api_key: str,
    api_url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    api_mode: str | None = None,
) -> list[str] | None:
    """A live catalog minus the models the endpoint marks unavailable now.

    maitred lists every model it fronts, loaded or not; the picker shows only
    what is being served. The catalog itself keeps every model, and naming an
    unlisted one still works.
    """
    if not models:
        return models
    from son_of_anton_cli.models import unavailable_models

    gone = unavailable_models(api_key, api_url, timeout=timeout, api_mode=api_mode, headers=headers)
    return [m for m in models if m not in gone] if gone else models


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------

@dataclass
class ModelSwitchResult:
    """Result of a model switch attempt."""

    success: bool
    new_model: str = ""
    target_provider: str = ""
    provider_changed: bool = False
    api_key: str = ""
    base_url: str = ""
    api_mode: str = ""
    error_message: str = ""
    warning_message: str = ""
    provider_label: str = ""
    is_global: bool = False


def resolve_persist_behavior(
    is_global: bool,
    is_session: bool,
    is_once: bool = False,
    explicit_provider: str = "",
) -> bool:
    """Whether a ``/model`` switch persists to config.toml.

    ``--once``/``--session`` never persist, ``--global`` always does, a
    ``--provider`` switch is session-only, otherwise
    ``model.persist_switch_by_default`` (default False) decides.
    """
    if is_once or is_session:
        return False
    if is_global:
        return True
    if explicit_provider:
        return False
    try:
        from son_of_anton_cli.config import load_config

        model_cfg = load_config().get("model")
        if isinstance(model_cfg, dict):
            return bool(model_cfg.get("persist_switch_by_default", False))
    except Exception:
        pass
    return False


MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL = "once_with_global"
MODEL_SWITCH_ERR_ONCE_REQUIRES_TARGET = "once_requires_target"
# Canonical error copy; surfaces add their own decoration.
MODEL_SWITCH_ERROR_TEXT = {
    MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL: "/model --once cannot be combined with --global",
    MODEL_SWITCH_ERR_ONCE_REQUIRES_TARGET: "/model --once requires a model or provider.",
}


@dataclass(frozen=True)
class ModelSwitchRequest:
    """A parsed /model command; ``errors`` holds ``MODEL_SWITCH_ERR_*`` codes."""

    raw: str
    target: str
    explicit_provider: str = ""
    is_global: bool = False
    is_session: bool = False
    is_once: bool = False
    force_refresh: bool = False
    show_all: bool = False
    scope: str = "default"
    errors: tuple = ()

    def error_messages(self) -> list:
        return [MODEL_SWITCH_ERROR_TEXT[code] for code in self.errors]


def parse_model_switch_args(raw: str) -> ModelSwitchRequest:
    """Parse ``/model`` arguments: ``--global``, ``--session``, ``--once``,
    ``--refresh``, ``--all``, ``--provider <slug>``; the rest is the model."""
    import re

    raw = str(raw or "")
    # Some clients auto-convert "--" to an em/en dash.
    normalized = re.sub(r"[‒–—―](provider|global|session|refresh|once|all)", r"--\1", raw)
    flags = {"--global": False, "--session": False, "--refresh": False, "--once": False, "--all": False}
    explicit_provider = ""
    parts = normalized.split()
    rest: list[str] = []
    i = 0
    while i < len(parts):
        part = "--all" if parts[i] == "--all-models" else parts[i]
        if part in flags:
            flags[part] = True
        elif part == "--provider" and i + 1 < len(parts):
            explicit_provider = parts[i + 1]
            i += 1
        else:
            rest.append(parts[i])
        i += 1
    target = " ".join(rest).strip()
    errors: list = []
    if flags["--once"] and flags["--global"]:
        errors.append(MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL)
    if flags["--once"] and not target and not explicit_provider:
        errors.append(MODEL_SWITCH_ERR_ONCE_REQUIRES_TARGET)
    scope = (
        "once" if flags["--once"] else "session" if flags["--session"]
        else "global" if flags["--global"] else "default"
    )
    return ModelSwitchRequest(
        raw=raw, target=target, explicit_provider=explicit_provider,
        is_global=flags["--global"], is_session=flags["--session"], is_once=flags["--once"],
        force_refresh=flags["--refresh"], show_all=flags["--all"], scope=scope, errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Context window shown after a switch
# ---------------------------------------------------------------------------

def resolve_display_context_length(
    model: str,
    provider: str,
    base_url: str = "",
    api_key: str = "",
    custom_providers: list | None = None,
    config_context_length: int | None = None,
    configured_model: str | None = None,
    configured_provider: str | None = None,
    configured_base_url: str | None = None,
) -> Optional[int]:
    """The context length to show in /model output (the endpoint's
    ``/models`` value unless a config pin for this route applies)."""
    if config_context_length is not None and (configured_model or configured_provider or configured_base_url):
        try:
            from son_of_anton_cli.route_identity import should_clear_context_pin

            if should_clear_context_pin(
                configured_model, model, configured_base_url, base_url, configured_provider, provider
            ):
                config_context_length = None
        except Exception:
            config_context_length = None
    try:
        from agent.model_metadata import get_model_context_length

        ctx = get_model_context_length(
            model,
            base_url=base_url or "",
            api_key=api_key or "",
            provider=provider or None,
            custom_providers=custom_providers,
            config_context_length=config_context_length,
        )
        return int(ctx) if ctx else None
    except Exception:
        return None


async def resolve_display_context_length_async(*args, **kwargs) -> Optional[int]:
    """:func:`resolve_display_context_length` off the event loop."""
    import asyncio

    return await asyncio.to_thread(resolve_display_context_length, *args, **kwargs)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def switch_model(
    raw_input: str,
    current_provider: str,
    current_model: str,
    current_base_url: str = "",
    current_api_key: str = "",
    is_global: bool = False,
    explicit_provider: str = "",
    user_providers: dict = None,
    custom_providers: list | None = None,
) -> ModelSwitchResult:
    """Resolve a /model request to a model, its endpoint and credential.

    ``--provider`` names a ``custom_providers`` entry; without it, a model
    some entry declares routes to that entry, anything else stays on the
    current one. The model is checked against the endpoint's ``/models``.
    """
    from son_of_anton_cli.runtime_provider import resolve_runtime_provider

    new_model = raw_input.strip()
    target_provider = current_provider
    entries = [e for e in (custom_providers or []) if isinstance(e, dict) and e.get("name")]

    def _entry_for(slug: str) -> Optional[dict]:
        wanted = str(slug or "").strip().lower()
        return next((e for e in entries if wanted in _entry_aliases(e)), None)

    if explicit_provider:
        entry = _entry_for(explicit_provider)
        if entry is None and not (explicit_provider.strip().lower() == "custom" and current_base_url):
            return ModelSwitchResult(
                success=False, is_global=is_global,
                error_message=(
                    f"Unknown provider '{explicit_provider}'. Declare it in config.toml "
                    f"under 'custom_providers'."
                ),
            )
        target_provider = _entry_slug(entry) if entry is not None else "custom"
        if not new_model:
            return ModelSwitchResult(
                success=False, target_provider=target_provider, is_global=is_global,
                error_message=f"Specify the model: /model <model-name> --provider {explicit_provider}",
            )
    else:
        wanted = new_model.lower()
        matches = {
            _entry_slug(e): m
            for e in entries
            for m in _declared_model_ids(e.get("models")) + _declared_model_ids(e.get("model"))
            if m.lower() == wanted
        }
        if matches and current_provider not in matches:
            if len(matches) > 1:
                return ModelSwitchResult(
                    success=False, is_global=is_global,
                    error_message=(
                        f"'{new_model}' is declared by multiple configured providers "
                        f"({', '.join(sorted(matches))}). Re-run with --provider <slug> to choose."
                    ),
                )
            target_provider, new_model = next(iter(matches.items()))
        elif matches:
            new_model = matches[current_provider]

    provider_changed = target_provider != current_provider
    target_entry = _entry_for(target_provider) if target_provider.startswith("custom:") else None
    provider_label = (
        str(target_entry.get("name")) if target_entry is not None
        else "Custom endpoint" if target_provider == "custom" and current_base_url
        else target_provider
    )

    api_key, base_url = current_api_key, current_base_url
    try:
        runtime = resolve_runtime_provider(
            requested=target_provider if (provider_changed or explicit_provider) else current_provider,
            target_model=new_model,
        )
        api_key, base_url = runtime.get("api_key", ""), runtime.get("base_url", "")
    except Exception as e:
        if provider_changed or explicit_provider:
            return ModelSwitchResult(
                success=False, target_provider=target_provider, provider_label=provider_label,
                is_global=is_global,
                error_message=f"Could not resolve credentials for provider '{provider_label}': {e}",
            )

    from son_of_anton_cli.models import validate_requested_model

    try:
        validation = validate_requested_model(new_model, target_provider, api_key=api_key, base_url=base_url)
    except Exception as e:
        validation = {"accepted": False, "message": f"Could not validate `{new_model}`: {e}"}
    if not validation.get("accepted"):
        # A model the entry declares is accepted even if /models omits it.
        declared = any(
            (target_provider.lower() in _entry_aliases(e) or e.get("base_url") == base_url)
            and (new_model == e.get("model") or new_model in _declared_model_ids(e.get("models")))
            for e in entries
        )
        if not declared:
            return ModelSwitchResult(
                success=False, new_model=new_model, target_provider=target_provider,
                provider_label=provider_label, is_global=is_global,
                error_message=validation.get("message", "Invalid model"),
            )
    if validation.get("corrected_model"):
        new_model = validation["corrected_model"]
    return ModelSwitchResult(
        success=True,
        new_model=new_model,
        target_provider=target_provider,
        provider_changed=provider_changed,
        api_key=api_key,
        base_url=base_url,
        api_mode="chat_completions",
        warning_message=validation.get("message") or "",
        provider_label=provider_label,
        is_global=is_global,
    )


# ---------------------------------------------------------------------------
# The picker's provider rows
# ---------------------------------------------------------------------------

_picker_prewarm_done = threading.Event()


def prewarm_picker_cache_async() -> Optional[threading.Thread]:
    """Warm the picker's model cache once, off-thread, so the first /model is instant."""
    if _picker_prewarm_done.is_set():
        return None
    _picker_prewarm_done.set()

    def _warm() -> None:
        try:
            from son_of_anton_cli.inventory import load_picker_context

            ctx = load_picker_context()
            list_authenticated_providers(
                current_provider=ctx.current_provider,
                current_base_url=ctx.current_base_url,
                current_model=ctx.current_model,
                custom_providers=ctx.custom_providers,
            )
        except Exception:
            logger.debug("picker cache prewarm failed", exc_info=True)

    t = threading.Thread(target=_warm, daemon=True, name="picker-cache-prewarm")
    t.start()
    return t


def list_authenticated_providers(
    current_provider: str = "",
    current_base_url: str = "",
    user_providers: dict = None,
    custom_providers: list | None = None,
    *,
    max_models: int | None = None,
    current_model: str = "",
    refresh: bool = False,
    probe_custom_providers: bool = True,
    probe_current_custom_provider: bool = False,
    show_all_models: bool = False,
    for_picker: bool = False,
    excluded_providers: list | None = None,
) -> List[dict]:
    """One picker row per ``custom_providers`` entry: ``slug``, ``name``,
    ``is_current``, ``models``, ``total_models``, ``api_url``, ``models_pinned``.

    A declared ``models`` subset pins the row unless ``discover_models`` is
    true or ``show_all_models`` (``/model --all``) asks for the full catalog.
    ``probe_*`` gate live ``/models`` probing (the cached catalog still
    answers when probing is off).
    """
    if refresh:
        from son_of_anton_cli.models import clear_provider_models_cache

        clear_provider_models_cache()
    current_norm = str(current_provider or "").strip().lower()
    current_url = str(current_base_url or "").strip().rstrip("/").lower()
    entries = [
        e for e in (custom_providers or [])
        if isinstance(e, dict) and str(e.get("name") or "").strip()
        and str(e.get("base_url") or e.get("url") or e.get("api") or "").strip()
    ]
    same_url_count = sum(
        1 for e in entries
        if current_url and str(e.get("base_url") or "").strip().rstrip("/").lower() == current_url
    )
    timeout = 1.5 if for_picker else 5.0
    results: List[dict] = []
    for entry in entries:
        api_url = str(entry.get("base_url") or entry.get("url") or entry.get("api") or "").strip().rstrip("/")
        api_key = str(entry.get("api_key") or "").strip() or _scoped_key_env(str(entry.get("key_env") or "").strip())
        slug = _entry_slug(entry)
        is_current = (
            current_norm in _entry_aliases(entry) | {slug}
            or (current_norm == "custom" and api_url.lower() == current_url and same_url_count == 1)
        )
        models = _declared_model_ids(entry.get("model")) + [
            m for m in _declared_model_ids(entry.get("models")) if m != entry.get("model")
        ]
        discover = entry.get("discover_models", True)
        if isinstance(discover, str):
            discover = discover.lower() not in {"false", "no", "0"}
        pinned = _entry_catalog_pinned(entry)
        has_explicit_list = isinstance(entry.get("models"), (list, tuple, str)) and bool(
            _declared_model_ids(entry.get("models"))
        ) and not _entry_models_discovered(entry)
        if discover and not (pinned and not show_all_models):
            try:
                if (api_key or not has_explicit_list) and (
                    probe_custom_providers or (probe_current_custom_provider and is_current)
                ):
                    live = _fetch_picker_live_models(api_key, api_url, timeout=timeout)
                    if live:
                        models = _drop_unavailable(live, api_key, api_url, timeout=timeout)
                else:
                    from son_of_anton_cli.models import cached_fetch_api_models

                    cached = cached_fetch_api_models(api_key, api_url, cache_only=True, timeout=timeout)
                    if cached:
                        models = cached
            except Exception:
                pass
        if current_model and is_current and current_model not in models:
            models = [current_model, *models]
        results.append({
            "slug": slug,
            "name": str(entry.get("name")).strip(),
            "is_current": is_current,
            "is_user_defined": True,
            "models": models,
            "total_models": len(models),
            "source": "user-config",
            "api_url": api_url,
            "models_pinned": pinned,
        })
    results.sort(key=lambda r: (not r["is_current"], -r["total_models"]))
    return results


def list_picker_providers(**kwargs) -> List[dict]:
    """Interactive-picker variant of :func:`list_authenticated_providers`."""
    return list_authenticated_providers(**kwargs, for_picker=True)
