"""Interactive prompt callbacks for terminal_tool integration.

The secret-capture prompt, bridged into the interactive front-end. It takes
the SonOfAntonCLI instance as its first argument and uses its state (queues,
app reference) to coordinate with the TUI. Clarify and approval prompts live
on SonOfAntonCLI itself.
"""

import queue
import time as _time

from son_of_anton_cli.banner import cprint, _DIM, _RST
from son_of_anton_cli.config import save_env_value_secure
from son_of_anton_cli.secret_prompt import masked_secret_prompt
from son_of_anton_constants import display_son_of_anton_home


def prompt_for_secret(cli, var_name: str, prompt: str, metadata=None) -> dict:
    """Prompt for a secret value through the TUI (e.g. API keys for skills).

    Returns a dict with keys: success, stored_as, validated, skipped, message.
    The secret is stored in ~/.son-of-anton/.env and never exposed to the model.
    """
    if not getattr(cli, "_app", None):
        if not hasattr(cli, "_secret_state"):
            cli._secret_state = None
        if not hasattr(cli, "_secret_deadline"):
            cli._secret_deadline = 0
        try:
            value = masked_secret_prompt(f"{prompt} (hidden, ESC or empty Enter to skip): ")
        except (EOFError, KeyboardInterrupt):
            value = ""

        if not value:
            cprint(f"\n{_DIM}  ⏭ Secret entry skipped{_RST}")
            return {
                "success": True,
                "reason": "cancelled",
                "stored_as": var_name,
                "validated": False,
                "skipped": True,
                "message": "Secret setup was skipped.",
            }

        stored = save_env_value_secure(var_name, value)
        _dhh = display_son_of_anton_home()
        cprint(f"\n{_DIM}  ✓ Stored secret in {_dhh}/.env as {var_name}{_RST}")
        return {
            **stored,
            "skipped": False,
            "message": "Secret stored securely. The secret value was not exposed to the model.",
        }

    timeout = 120
    response_queue = queue.Queue()

    cli._secret_state = {
        "var_name": var_name,
        "prompt": prompt,
        "metadata": metadata or {},
        "response_queue": response_queue,
    }
    cli._secret_deadline = _time.monotonic() + timeout
    # Avoid storing stale draft input as the secret when Enter is pressed.
    if hasattr(cli, "_clear_secret_input_buffer"):
        try:
            cli._clear_secret_input_buffer()
        except Exception:
            pass
    elif hasattr(cli, "_app") and cli._app:
        try:
            cli._app.current_buffer.reset()
        except Exception:
            pass

    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()

    while True:
        try:
            value = response_queue.get(timeout=1)
            cli._secret_state = None
            cli._secret_deadline = 0
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()

            if not value:
                cprint(f"\n{_DIM}  ⏭ Secret entry skipped{_RST}")
                return {
                    "success": True,
                    "reason": "cancelled",
                    "stored_as": var_name,
                    "validated": False,
                    "skipped": True,
                    "message": "Secret setup was skipped.",
                }

            stored = save_env_value_secure(var_name, value)
            _dhh = display_son_of_anton_home()
            cprint(f"\n{_DIM}  ✓ Stored secret in {_dhh}/.env as {var_name}{_RST}")
            return {
                **stored,
                "skipped": False,
                "message": "Secret stored securely. The secret value was not exposed to the model.",
            }
        except queue.Empty:
            remaining = cli._secret_deadline - _time.monotonic()
            if remaining <= 0:
                break
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()

    cli._secret_state = None
    cli._secret_deadline = 0
    if hasattr(cli, "_clear_secret_input_buffer"):
        try:
            cli._clear_secret_input_buffer()
        except Exception:
            pass
    elif hasattr(cli, "_app") and cli._app:
        try:
            cli._app.current_buffer.reset()
        except Exception:
            pass
    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()
    cprint(f"\n{_DIM}  ⏱ Timeout — secret capture cancelled{_RST}")
    return {
        "success": True,
        "reason": "timeout",
        "stored_as": var_name,
        "validated": False,
        "skipped": True,
        "message": "Secret setup timed out and was skipped.",
    }


