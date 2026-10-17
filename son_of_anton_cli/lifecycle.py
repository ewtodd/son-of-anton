"""Son of Anton lifecycle dispatch to plugin hooks.

``son_of_anton_cli.plugins`` is imported lazily so that call sites on hot
startup paths do not pay for plugin-manager import until a hook fires.
"""

from __future__ import annotations

from typing import Any, List


def invoke_hook(hook_name: str, **kwargs: Any) -> List[Any]:
    """Invoke plugin hooks registered for ``hook_name``."""
    from son_of_anton_cli import plugins

    return plugins.invoke_hook(hook_name, **kwargs)


def has_hook(hook_name: str) -> bool:
    """Return whether a plugin consumes ``hook_name``."""
    from son_of_anton_cli import plugins

    return plugins.has_hook(hook_name)


def finalize_session(**kwargs: Any) -> List[Any]:
    """Notify plugins that one session ended for good."""
    from son_of_anton_cli import plugins

    return plugins.invoke_hook("on_session_finalize", **kwargs)
