"""Shared CLI output helpers for Son of Anton CLI modules.

Extracts the identical ``print_info/success/warning/error`` and ``prompt()``
functions previously duplicated across tools_config.py, mcp_config.py,
memory_setup.py and the removed setup wizard, plus its prompt_choice menu.
"""


from son_of_anton_cli.colors import Colors, color
from son_of_anton_cli.secret_prompt import masked_secret_prompt


# ─── Print Helpers ────────────────────────────────────────────────────────────


def print_info(text: str) -> None:
    """Print a dim informational message."""
    print(color(f"  {text}", Colors.DIM))


def print_success(text: str) -> None:
    """Print a green success message with ✓ prefix."""
    print(color(f"✓ {text}", Colors.GREEN))


def print_error(text: str) -> None:
    """Print a red error message with ✗ prefix."""
    print(color(f"✗ {text}", Colors.RED))


# ─── Input Prompts ────────────────────────────────────────────────────────────


def line_input(prompt_text: str) -> str:
    """Read one line of non-secret text.

    Used by setup and model-selection commands, which run before (or outside)
    the interactive app, so plain ``input`` is safe: nothing else owns the tty.
    """
    return input(prompt_text)


def prompt(
    question: str,
    default: str | None = None,
    password: bool = False,
) -> str:
    """Prompt the user for input with optional default and password masking.

    Replaces the four independent ``_prompt()`` / ``prompt()`` implementations
    in setup.py, tools_config.py, mcp_config.py, and memory_setup.py.

    Returns the user's input (stripped), or *default* if the user presses Enter.
    Returns empty string on Ctrl-C or EOF.
    """
    suffix = f" [{default}]" if default else ""
    display = color(f"  {question}{suffix}: ", Colors.YELLOW)

    try:
        if password:
            value = masked_secret_prompt(display)
        else:
            value = line_input(display)
        value = value.strip()
        return value if value else (default or "")
    except (KeyboardInterrupt, EOFError):
        print()
        return ""

