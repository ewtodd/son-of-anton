"""Per-provider model-selection wizard flows for ``son-of-anton setup`` / ``son-of-anton model``.

Extracted from ``son_of_anton_cli/main.py`` as part of the god-file decomposition
campaign. The fork keeps three flows, dispatched by ``select_provider_and_model``
(which stays in main.py):

- ``_model_flow_custom`` — manual custom endpoint (URL + key + model)
- ``_model_flow_named_custom`` — a saved ``custom_providers`` / ``providers`` entry
- ``_model_flow_api_key_provider`` — generic flow for API-key providers
  (deepseek, openai-api, and any plugin-registered api_key profile)

main.py-internal helpers the flows call (``_prompt_api_key``,
``_save_custom_provider``, …) are imported lazily inside the flows (``from
son_of_anton_cli.main import ...`` resolves at call time, when main.py is fully
loaded) so this module never imports ``son_of_anton_cli.main`` at import time
-> no import cycle.
"""

from __future__ import annotations
from son_of_anton_cli.cli_output import line_input

import os
import subprocess
import urllib.parse

from son_of_anton_cli.config import clear_model_endpoint_credentials
from son_of_anton_cli.providers import custom_provider_slug
def _existing_api_key_for_model_flow(provider_id: str, pconfig) -> tuple[str, str]:
    """Resolve an existing wizard credential without changing its storage."""
    from son_of_anton_cli.auth import _resolve_api_key_provider_secret

    return _resolve_api_key_provider_secret(provider_id, pconfig)


def _prune_replaced_custom_model_config_credentials(
    base_url: str,
    *,
    provider_name: str = "",
) -> None:
    """Drop stale ``model_config`` credentials from inactive custom pools.

    ``model_config`` means "the credential currently stored under
    ``model.api_key``". After an explicit custom-endpoint switch, any old
    custom pool still carrying that source points at the previous endpoint and
    can be selected before the freshly saved config is tried.
    """
    try:
        from agent.credential_pool import (
            CUSTOM_POOL_PREFIX,
            get_custom_provider_pool_key,
        )
        from son_of_anton_cli.auth import read_credential_pool, write_credential_pool

        active_pool_key = get_custom_provider_pool_key(
            base_url,
            provider_name=provider_name or None,
        )
        if not active_pool_key:
            return
        pools = read_credential_pool(None)
        if not isinstance(pools, dict):
            return
        for pool_key, entries in pools.items():
            if (
                not isinstance(pool_key, str)
                or not pool_key.startswith(CUSTOM_POOL_PREFIX)
                or pool_key == active_pool_key
                or not isinstance(entries, list)
            ):
                continue
            retained = []
            removed_ids = []
            changed = False
            for entry in entries:
                if isinstance(entry, dict) and entry.get("source") == "model_config":
                    changed = True
                    entry_id = entry.get("id")
                    if entry_id:
                        removed_ids.append(str(entry_id))
                    continue
                retained.append(entry)
            if changed:
                write_credential_pool(pool_key, retained, removed_ids=removed_ids)
    except Exception:
        return


def _model_flow_custom(config):
    """Custom endpoint: collect URL, API key, and model name.

    Automatically saves the endpoint to ``custom_providers`` in config.yaml
    so it appears in the provider menu on subsequent runs.
    """
    from son_of_anton_cli.main import _auto_provider_name, _prompt_custom_api_mode_selection, _save_custom_provider
    from son_of_anton_cli.auth import _save_model_choice, deactivate_provider
    from son_of_anton_cli.config import (
        custom_endpoint_key_env,
        get_env_value,
        load_config,
        save_config,
        save_env_value,
    )
    from son_of_anton_cli.secret_prompt import masked_secret_prompt

    current_url = get_env_value("OPENAI_BASE_URL") or ""
    current_key = get_env_value("OPENAI_API_KEY") or ""

    print("Custom OpenAI-compatible endpoint configuration:")
    if current_url:
        print(f"  Current URL: {current_url}")
    if current_key:
        print(f"  Current key: {current_key[:8]}...")
    print()

    try:
        base_url = line_input(
            f"API base URL [{current_url or 'e.g. https://api.example.com/v1'}]: "
        ).strip()
        api_key = masked_secret_prompt(
            f"API key [{current_key[:8] + '...' if current_key else 'optional'}]: "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return

    if not base_url and not current_url:
        print("No URL provided. Cancelled.")
        return

    # Validate URL format
    effective_url = base_url or current_url
    if not effective_url.startswith(("http://", "https://")):
        print(f"Invalid URL: {effective_url} (must start with http:// or https://)")
        return

    effective_key = api_key or current_key

    # Hint: most local model servers (Ollama, vLLM, llama.cpp) require /v1
    # in the base URL for OpenAI-compatible chat completions.  Prompt the
    # user if the URL looks like a local server without /v1.
    _url_lower = effective_url.rstrip("/").lower()
    _looks_local = any(
        h in _url_lower
        for h in ("localhost", "127.0.0.1", "0.0.0.0", ":11434", ":8080", ":5000")
    )
    if _looks_local and not _url_lower.endswith("/v1"):
        print()
        print("  Hint: Did you mean to add /v1 at the end?")
        print("  Most local model servers (Ollama, vLLM, llama.cpp) require it.")
        print(f"  e.g. {effective_url.rstrip('/')}/v1")
        try:
            _add_v1 = input("  Add /v1? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            _add_v1 = "n"
        if _add_v1 in {"", "y", "yes"}:
            effective_url = effective_url.rstrip("/") + "/v1"
            if base_url:
                base_url = effective_url
            print(f"  Updated URL: {effective_url}")
        print()

    from son_of_anton_cli.models import probe_api_models

    probe = probe_api_models(effective_key, effective_url)
    if probe.get("used_fallback") and probe.get("resolved_base_url"):
        print(
            f"Warning: endpoint verification worked at {probe['resolved_base_url']}/models, "
            f"not the exact URL you entered. Saving the working base URL instead."
        )
        effective_url = probe["resolved_base_url"]
        if base_url:
            base_url = effective_url
    elif probe.get("models") is not None:
        print(
            f"Verified endpoint via {probe.get('probed_url')} "
            f"({len(probe.get('models') or [])} model(s) visible)"
        )
    else:
        print(
            f"Warning: could not verify this endpoint via {probe.get('probed_url')}. "
            f"Son of Anton will still save it."
        )
        if probe.get("suggested_base_url"):
            suggested = probe["suggested_base_url"]
            if suggested.endswith("/v1"):
                print(
                    f"  If this server expects /v1 in the path, try base URL: {suggested}"
                )
            else:
                print(f"  If /v1 should not be in the base URL, try: {suggested}")

    # Prompt for API compatibility mode explicitly so codex-compatible custom
    # providers don't silently fall back to chat_completions.
    current_model_cfg = config.get("model")
    current_api_mode = ""
    if isinstance(current_model_cfg, dict):
        current_api_mode = str(current_model_cfg.get("api_mode") or "").strip()
    api_mode = _prompt_custom_api_mode_selection(
        effective_url,
        current_api_mode=current_api_mode,
    )
    if api_mode:
        print(f"  API mode: {api_mode}")
    else:
        print("  API mode: auto-detect")

    # Select model — use probe results when available, fall back to manual input
    model_name = ""
    detected_models = probe.get("models") or []
    try:
        if len(detected_models) == 1:
            print(f"  Detected model: {detected_models[0]}")
            confirm = input("  Use this model? [Y/n]: ").strip().lower()
            if confirm in {"", "y", "yes"}:
                model_name = detected_models[0]
            else:
                model_name = line_input("Model name (e.g. gpt-4, llama-3-70b): ").strip()
        elif len(detected_models) > 1:
            print("  Available models:")
            for i, m in enumerate(detected_models, 1):
                print(f"    {i}. {m}")
            pick = input(
                f"  Select model [1-{len(detected_models)}] or type name: "
            ).strip()
            if pick.isdigit() and 1 <= int(pick) <= len(detected_models):
                model_name = detected_models[int(pick) - 1]
            elif pick:
                model_name = pick
        else:
            model_name = line_input("Model name (e.g. gpt-4, llama-3-70b): ").strip()

        context_length_str = line_input(
            "Context length in tokens [leave blank for auto-detect]: "
        ).strip()

        # Prompt for a display name — shown in the provider menu on future runs
        default_name = _auto_provider_name(effective_url)
        display_name = line_input(f"Display name [{default_name}]: ").strip() or default_name
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return

    context_length = None
    if context_length_str:
        try:
            context_length = int(
                context_length_str.replace(",", "")
                .replace("k", "000")
                .replace("K", "000")
            )
            if context_length <= 0:
                context_length = None
        except ValueError:
            print(f"Invalid context length: {context_length_str} — will auto-detect.")
            context_length = None

    # The key goes to .env and config.yaml only references it (#69449). Keyed
    # on host:port so two servers on one machine keep separate credentials.
    custom_key_env = ""
    if effective_key:
        _parsed = urllib.parse.urlparse(effective_url)
        _identity = _parsed.hostname or ""
        if _parsed.port:
            _identity = f"{_identity}_{_parsed.port}"
        custom_key_env = custom_endpoint_key_env(_identity)
        save_env_value(custom_key_env, effective_key)
        print(f"  API key saved to .env as {custom_key_env}")

    if model_name:
        _save_model_choice(model_name)

        # Update config and deactivate any OAuth provider
        cfg = load_config()
        model = cfg.get("model")
        if not isinstance(model, dict):
            model = {"default": model} if model else {}
            cfg["model"] = model
        model["provider"] = "custom"
        model["base_url"] = effective_url
        if custom_key_env:
            model["api_key"] = f"${{{custom_key_env}}}"
        if api_mode:
            model["api_mode"] = api_mode
        else:
            model.pop("api_mode", None)
        save_config(cfg)
        deactivate_provider()

        # Sync the caller's config dict so the setup wizard's final
        # save_config(config) preserves our model settings.  Without
        # this, the wizard overwrites model.provider/base_url with
        # the stale values from its own config dict (#4172).
        config["model"] = dict(model)

        print(f"Default model set to: {model_name} (via {effective_url})")
    else:
        if base_url or api_key:
            deactivate_provider()
        # Even without a model name, persist the custom endpoint on the
        # caller's config dict so the setup wizard doesn't lose it.
        _caller_model = config.get("model")
        if not isinstance(_caller_model, dict):
            _caller_model = {"default": _caller_model} if _caller_model else {}
        _caller_model["provider"] = "custom"
        _caller_model["base_url"] = effective_url
        if custom_key_env:
            _caller_model["api_key"] = f"${{{custom_key_env}}}"
        if api_mode:
            _caller_model["api_mode"] = api_mode
        else:
            _caller_model.pop("api_mode", None)
        config["model"] = _caller_model
        print("Endpoint saved. Use `/model` in chat or `son-of-anton model` to set a model.")

    # Auto-save to custom_providers so it appears in the menu next time
    _save_custom_provider(
        effective_url,
        effective_key,
        model_name or "",
        context_length=context_length,
        name=display_name,
        api_mode=api_mode,
        key_env=custom_key_env,
    )
    _prune_replaced_custom_model_config_credentials(
        effective_url,
        provider_name=display_name,
    )


def _model_flow_named_custom(config, provider_info):
    """Handle a named custom provider from config.yaml custom_providers list.

    Probes the endpoint's model catalog to let the user pick a model, using
    native ``/api/tags`` for endpoints conservatively identified as Ollama.
    If a model was previously saved, it is pre-selected in the menu.
    Falls back to the saved model if probing fails.
    """
    from son_of_anton_cli.main import _custom_provider_api_key_config_value, _custom_provider_base_url_config_value, _save_custom_provider
    from son_of_anton_cli.auth import _save_model_choice, deactivate_provider
    from son_of_anton_cli.config import load_config, normalize_extra_headers, save_config
    from son_of_anton_cli.model_switch import (
        _entry_models_discovered,
        _models_config_is_allowlist,
    )
    from son_of_anton_cli.models import (
        fetch_api_models,
        fetch_ollama_local_models,
        _get_ollama_native_headers,
        _normalize_openai_base_url,
        should_use_ollama_native_catalog,
    )

    name = provider_info["name"]
    base_url = provider_info["base_url"]
    api_mode = provider_info.get("api_mode", "")
    api_key = provider_info.get("api_key", "")
    key_env = provider_info.get("key_env", "")
    saved_model = provider_info.get("model", "")
    provider_key = (provider_info.get("provider_key") or "").strip()

    # Resolve key from env var if api_key not set directly
    if not api_key and key_env:
        api_key = os.environ.get(key_env, "")
    config_api_key = _custom_provider_api_key_config_value(provider_info, api_key)

    # Honor ``discover_models: false`` (default True) — when discovery is
    # disabled, use the configured ``models:`` list verbatim and skip the
    # live /models probe. This lets operators restrict the picker to the
    # subset their plan actually serves instead of the endpoint's full
    # catalog (#18726: Baidu Qianfan returns 100+ models for a 2-3 model
    # plan). Same semantics as the slash-command picker (model_switch.py
    # sections 3 & 4): default discovers, false keeps the explicit list.
    discover = provider_info.get("discover_models", True)
    if isinstance(discover, str):
        discover = discover.lower() not in {"false", "no", "0"}
    configured_models: list[str] = []
    native_catalog_empty = False
    cfg_models = provider_info.get("models", {})
    explicit_catalog = _models_config_is_allowlist(
        cfg_models, _entry_models_discovered(provider_info)
    )
    if isinstance(cfg_models, dict):
        configured_models = [
            str(m)
            for m in cfg_models
            if m not in {
                "__explicit_model_allowlist__",
                "__discovered_model_catalog__",
            }
            and str(m).strip()
        ]
    elif isinstance(cfg_models, list):
        configured_models = []
        for model_entry in cfg_models:
            if isinstance(model_entry, dict):
                model_id = str(model_entry.get("id") or model_entry.get("model") or "").strip()
            else:
                model_id = str(model_entry).strip() if isinstance(model_entry, str) else ""
            if model_id:
                configured_models.append(model_id)

    print(f"  Provider: {name}")
    print(f"  URL:      {base_url}")
    if saved_model:
        print(f"  Current:  {saved_model}")
    print()

    if not discover:
        # Discovery disabled: never probe, even when only the singular active
        # model is configured. The active model is useful as the sole picker
        # choice, but it is not an endpoint catalog.
        models = configured_models or ([saved_model] if saved_model else [])
        print(
            "Using configured models (discover_models: false): "
            f"{len(models)}"
        )
    else:
        print("Fetching available models...")
        fetch_kwargs = {"timeout": 8.0}
        if api_mode:
            fetch_kwargs["api_mode"] = api_mode
        native_catalog_provider = (
            "ollama"
            if provider_key.lower() == "ollama" or name.strip().lower() == "ollama"
            else "custom"
        )
        extra_headers = normalize_extra_headers(provider_info.get("extra_headers")) or {}
        candidate_headers = _get_ollama_native_headers(base_url, api_key=api_key)
        for key in tuple(candidate_headers):
            if any(key.lower() == existing.lower() for existing in extra_headers):
                del candidate_headers[key]
        candidate_headers.update(extra_headers)
        caller_has_authorization = any(
            key.lower() == "authorization" for key in extra_headers
        )
        if api_key and not caller_has_authorization:
            for key in tuple(candidate_headers):
                if key.lower() == "authorization":
                    del candidate_headers[key]
            candidate_headers["Authorization"] = f"Bearer {api_key}"
        use_native = should_use_ollama_native_catalog(
            native_catalog_provider, base_url, headers=candidate_headers or None
        )
        native_headers_arg = candidate_headers or None if use_native else (extra_headers or None)
        explicit_allowlist = explicit_catalog
        if use_native:
            if explicit_catalog and configured_models:
                live_models = configured_models
                native_catalog_empty = False
            else:
                live_models = fetch_ollama_local_models(
                    base_url,
                    timeout=8.0,
                    headers=native_headers_arg,
                )
                native_catalog_empty = live_models == []
                if live_models is None:
                    live_models = fetch_api_models(
                        api_key,
                        _normalize_openai_base_url(base_url),
                        headers=native_headers_arg,
                        **fetch_kwargs,
                    )
                    native_catalog_empty = False
        else:
            live_models = fetch_api_models(
                api_key, base_url, headers=native_headers_arg, **fetch_kwargs
            )
            native_catalog_empty = False
        models = (
            configured_models
            if explicit_allowlist
            else []
            if native_catalog_empty
            else (live_models or configured_models)
        )
        # Persist the live catalog back to the custom_providers entry so that
        # no-probe surfaces (dashboard, desktop, ACP) show the full model list
        # instead of collapsing to the single ``model:`` default. Mirrors the
        # picker path in model_switch.py::_save_discovered_models_to_config; a
        # failed save is non-fatal.
        if live_models:
            try:
                from son_of_anton_cli.model_switch import (
                    _save_discovered_models_to_config,
                )

                _save_discovered_models_to_config(
                    base_url,
                    live_models,
                    api_mode=api_mode,
                    headers=extra_headers or None,
                )
            except Exception:
                pass

    if models:
        default_idx = 0
        if saved_model and saved_model in models:
            default_idx = models.index(saved_model)

        print(f"Found {len(models)} model(s):\n")
        try:
            from son_of_anton_cli.curses_ui import curses_radiolist

            menu_items = [
                f"{m} (current)" if m == saved_model else m for m in models
            ] + ["Cancel"]
            idx = curses_radiolist(
                f"Select model from {name}:",
                menu_items,
                selected=default_idx,
                cancel_returns=-1,
                searchable=True,
            )
            print()
            if idx < 0 or idx >= len(models):
                print("Cancelled.")
                return
            model_name = models[idx]
        except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
            for i, m in enumerate(models, 1):
                suffix = " (current)" if m == saved_model else ""
                print(f"  {i}. {m}{suffix}")
            print(f"  {len(models) + 1}. Cancel")
            print()
            try:
                val = input(f"Choice [1-{len(models) + 1}]: ").strip()
                if not val:
                    print("Cancelled.")
                    return
                idx = int(val) - 1
                if idx < 0 or idx >= len(models):
                    print("Cancelled.")
                    return
                model_name = models[idx]
            except (ValueError, KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return
    elif saved_model and not native_catalog_empty:
        print("Could not fetch models from endpoint.")
        try:
            model_name = line_input(f"Model name [{saved_model}]: ").strip() or saved_model
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return
    else:
        print("Could not fetch models from endpoint. Enter model name manually.")
        try:
            model_name = line_input("Model name: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return
        if not model_name:
            print("No model specified. Cancelled.")
            return

    # Activate and save the model to the custom_providers entry
    _save_model_choice(model_name)

    cfg = load_config()
    model = cfg.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if model else {}
        cfg["model"] = model
    if provider_key:
        model["provider"] = custom_provider_slug(name, provider_key)
        model.pop("base_url", None)
        model.pop("api_key", None)
    else:
        model["provider"] = "custom"
        model["base_url"] = _custom_provider_base_url_config_value(
            provider_info, base_url
        )
        if config_api_key:
            model["api_key"] = config_api_key
    # Apply api_mode from custom_providers entry, or clear stale value
    custom_api_mode = provider_info.get("api_mode", "")
    if custom_api_mode:
        model["api_mode"] = custom_api_mode
    else:
        model.pop("api_mode", None)  # let runtime auto-detect from URL
    save_config(cfg)
    deactivate_provider()

    # Persist the selected model back to whichever schema owns this endpoint.
    if provider_key:
        cfg = load_config()
        providers_cfg = cfg.get("providers")
        if isinstance(providers_cfg, dict):
            provider_entry = providers_cfg.get(provider_key)
            if isinstance(provider_entry, dict):
                provider_entry["default_model"] = model_name
                # Only persist an inline api_key when the user originally had
                # one (either a literal secret or a ``${VAR}`` template). When
                # the entry relies on ``key_env``, do not synthesize a
                # ``${key_env}`` api_key — the runtime already resolves the
                # key from ``key_env`` directly, and writing the resolved
                # secret (or even a synthesized template) would silently
                # downgrade credential hygiene on entries that intentionally
                # keep plaintext out of ``config.yaml``. See issue #15803.
                original_api_key_ref = str(
                    provider_info.get("api_key_ref", "") or ""
                ).strip()
                original_api_key = str(provider_info.get("api_key", "") or "").strip()
                had_inline_api_key = bool(original_api_key_ref or original_api_key)
                if (
                    had_inline_api_key
                    and config_api_key
                    and not str(provider_entry.get("api_key", "") or "").strip()
                ):
                    provider_entry["api_key"] = config_api_key
                if key_env and not str(provider_entry.get("key_env", "") or "").strip():
                    provider_entry["key_env"] = key_env
                cfg["providers"] = providers_cfg
                save_config(cfg)
    else:
        # Save model name to the custom_providers entry for next time
        _save_custom_provider(base_url, config_api_key, model_name, api_mode=api_mode)

    print(f"\n✅ Model set to: {model_name}")
    print(f"   Provider: {name} ({base_url})")

def _model_flow_api_key_provider(config, provider_id, current_model=""):
    """Generic flow for API-key providers (deepseek, openai-api, plugin profiles)."""
    from son_of_anton_cli.auth import (
        PROVIDER_REGISTRY,
        _prompt_model_selection,
        _save_model_choice,
        deactivate_provider,
    )
    from son_of_anton_cli.config import (
        get_env_value,
        save_env_value,
        load_config,
        save_config,
    )
    from son_of_anton_cli.models import (
        _PROVIDER_MODELS,
        fetch_api_models,
    )

    pconfig = PROVIDER_REGISTRY[provider_id]
    key_env = pconfig.api_key_env_vars[0] if pconfig.api_key_env_vars else ""
    base_url_env = pconfig.base_url_env_var or ""

    # Check / prompt for API key
    existing_key, existing_source = _existing_api_key_for_model_flow(provider_id, pconfig)
    existing_key, abort = _prompt_api_key(
        pconfig,
        existing_key,
        provider_id=provider_id,
        existing_source=existing_source,
    )
    if abort:
        return

    # Optional base URL override.
    # Precedence: env var → config.yaml model.base_url → registry default.
    # Reading config.yaml prevents silently overwriting a saved remote URL
    # (e.g. a remote LM Studio endpoint) with localhost when the user just
    # presses Enter at the prompt below.
    current_base = ""
    if base_url_env:
        current_base = get_env_value(base_url_env) or os.getenv(base_url_env, "")
    if not current_base:
        try:
            _m = load_config().get("model") or {}
            if str(_m.get("provider") or "").strip().lower() == provider_id:
                current_base = str(_m.get("base_url") or "").strip()
        except Exception:
            pass
    effective_base = current_base or pconfig.inference_base_url

    try:
        override = line_input(f"Base URL [{effective_base}]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        override = ""
    if override and base_url_env:
        if not override.startswith(("http://", "https://")):
            print(
                "  Invalid URL — must start with http:// or https://. Keeping current value."
            )
        else:
            save_env_value(base_url_env, override)
            effective_base = override

    # Model selection — resolution order:
    #   1. models.dev registry (cached, filtered for agentic/tool-capable models)
    #   2. Curated static fallback list (offline insurance)
    #   3. Live /models endpoint probe (small providers without models.dev data)
    curated = _PROVIDER_MODELS.get(provider_id, [])

    # Try models.dev first — returns tool-capable models, filtered for noise
    mdev_models: list = []
    try:
        from agent.models_dev import list_agentic_models

        mdev_models = list_agentic_models(provider_id)
    except Exception:
        pass

    if mdev_models:
        # Merge models.dev with curated list so newly added models
        # (not yet in models.dev) still appear in the picker.
        if curated:
            seen = {m.lower() for m in mdev_models}
            merged = list(mdev_models)
            for m in curated:
                if m.lower() not in seen:
                    merged.append(m)
                    seen.add(m.lower())
            model_list = merged
        else:
            model_list = mdev_models
        print(f"  Found {len(model_list)} model(s) from models.dev registry")
    elif curated and len(curated) >= 8:
        # Curated list is substantial — use it directly, skip live probe
        model_list = curated
        print(
            f'  Showing {len(model_list)} curated models — use "Enter custom model name" for others.'
        )
    else:
        api_key_for_probe = existing_key or (
            get_env_value(key_env) if key_env else ""
        )
        live_models = fetch_api_models(api_key_for_probe, effective_base)
        if live_models and len(live_models) >= len(curated):
            model_list = live_models
            print(f"  Found {len(model_list)} model(s) from {pconfig.name} API")
        else:
            model_list = curated
            if model_list:
                print(
                    f'  Showing {len(model_list)} curated models — use "Enter custom model name" for others.'
                )
        # else: no defaults either, will fall through to raw input

    if model_list:
        # Per-model pricing, when the provider supports it (via the models.dev
        # disk cache or cached /models endpoints). get_pricing_for_provider()
        # is memoized in-process and returns {} for providers without pricing
        # — never a blocking fetch beyond the catalog lookup above.
        pricing: dict = {}
        try:
            from son_of_anton_cli.models import get_pricing_for_provider

            pricing = get_pricing_for_provider(provider_id) or {}
        except Exception:
            pricing = {}
        selected = _prompt_model_selection(
            model_list,
            current_model=current_model,
            pricing=pricing,
            confirm_provider=provider_id,
            confirm_base_url=effective_base,
            confirm_api_key=existing_key,
        )
    else:
        try:
            selected = line_input("Model name: ").strip()
        except (KeyboardInterrupt, EOFError):
            selected = None

    if selected:
        _save_model_choice(selected)

        # Update config with provider, base URL, and API mode
        cfg = load_config()
        model = cfg.get("model")
        if not isinstance(model, dict):
            model = {"default": model} if model else {}
            cfg["model"] = model
        model["provider"] = provider_id
        model["base_url"] = effective_base
        clear_model_endpoint_credentials(model, clear_api_mode=False)
        model.pop("api_mode", None)
        save_config(cfg)
        deactivate_provider()

        print(f"Default model set to: {selected} (via {pconfig.name})")
    else:
        print("No change.")
