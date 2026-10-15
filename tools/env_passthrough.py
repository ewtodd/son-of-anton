"""Environment variable passthrough allowlist.

Both sandboxes (execute_code, terminal) strip secrets from the child process
environment by default. ``terminal.env_passthrough`` in config.toml lets users
explicitly allowlist vars that should reach the child anyway.

Both ``code_execution_tool.py`` and ``tools/environments/local.py`` consult
:func:`is_env_passthrough` before stripping a variable.
"""

from __future__ import annotations

import logging
from son_of_anton_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Cache for the config-based allowlist (loaded once per process).
_config_passthrough: frozenset[str] | None = None


def _is_son_of_anton_provider_credential(name: str) -> bool:
    """True if ``name`` is a Son of Anton-managed provider credential (API key,
    token, or similar) per ``_SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST``.

    The passthrough allowlist must not be able to override this list — that
    was the bypass in GHSA-rhgp-j443-p4rf, where a passthrough registration of
    ``ANTHROPIC_TOKEN`` / ``OPENAI_API_KEY`` handed the credential to the
    ``execute_code`` child process, defeating the sandbox's scrubbing
    guarantee. Non-Son of Anton API keys (TENOR_API_KEY, NOTION_TOKEN, etc.)
    are NOT in the blocklist and remain legitimately allowlistable.

    Fail closed: if the authoritative blocklist cannot be imported (partial
    install, import-time error, etc.) we treat the name as a protected
    provider credential and refuse passthrough, rather than fall open.
    """
    try:
        from tools.environments.local import (
            _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST,
            _is_son_of_anton_internal_secret,
        )
    except Exception as e:
        logger.warning(
            "env passthrough: provider credential blocklist import failed; "
            "failing closed and refusing passthrough registration for %r: %s",
            name,
            e,
        )
        return True
    # Dynamically-generated Son of Anton-internal secrets (AUXILIARY_*_API_KEY /
    # _BASE_URL side-LLM credentials, GATEWAY_RELAY_* relay-auth) are provider
    # credentials the static blocklist can't enumerate — they're injected per
    # task/relay at gateway startup. They must never be allowlisted into an
    # execute_code / terminal child.
    if _is_son_of_anton_internal_secret(name):
        return True
    return name in _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST


def _load_config_passthrough() -> frozenset[str]:
    """Load ``tools.env_passthrough`` from config.toml (cached)."""
    global _config_passthrough
    if _config_passthrough is not None:
        return _config_passthrough

    result: set[str] = set()
    try:
        from son_of_anton_cli.config import read_raw_config
        cfg = read_raw_config()
        passthrough = cfg_get(cfg, "terminal", "env_passthrough")
        if isinstance(passthrough, list):
            for item in passthrough:
                if not isinstance(item, str) or not item.strip():
                    continue
                name = item.strip()
                # Son of Anton-managed provider credentials must not be passed
                # through to execute_code / terminal children, even when
                # config.toml asks for it. See GHSA-rhgp-j443-p4rf.
                if _is_son_of_anton_provider_credential(name):
                    logger.warning(
                        "env passthrough: refusing to register Son of Anton "
                        "provider credential %r from config.toml (blocked "
                        "by _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST). Operator "
                        "configuration must not override the execute_code "
                        "sandbox's credential scrubbing; see "
                        "GHSA-rhgp-j443-p4rf.",
                        name,
                    )
                    continue
                result.add(name)
    except Exception as e:
        logger.debug("Could not read tools.env_passthrough from config: %s", e)

    _config_passthrough = frozenset(result)
    return _config_passthrough


def is_env_passthrough(var_name: str) -> bool:
    """Check whether *var_name* is allowed to pass through to sandboxes.

    Returns ``True`` if the variable is listed in the user's
    ``terminal.env_passthrough`` config.
    """
    return var_name in _load_config_passthrough()


def get_all_passthrough() -> frozenset[str]:
    """Return the config-based passthrough vars."""
    return _load_config_passthrough()


def resolve_passthrough_value(
    name: str,
    fallback: str | None = None,
) -> str | None:
    """Resolve an allowlisted variable, preferring the caller's own value.

    ``fallback`` is the value the caller already resolved (typically a
    snapshot of ``os.environ`` or the home's ``.env``), including any explicit
    per-call override, so it wins when present. Otherwise read the process
    environment through the credential seam.
    """
    from agent.secret_scope import get_secret

    if fallback is not None:
        return fallback
    return get_secret(name)
