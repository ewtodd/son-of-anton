"""
Interactive setup wizard for Son of Anton Agent.

Modular wizard with independently-runnable sections:
  1. Model & Provider — choose your AI provider and model
  2. Terminal Backend — where your agent runs commands
  3. Agent Settings — iterations, compression, session reset
  4. Messaging Platforms — connect Discord, Slack, Signal, etc.
  5. Tools — configure web search, vision, skills, etc.

Config files are stored in ~/.son-of-anton/ for easy access.
"""

import importlib.util
import logging
import os
import re
import shutil
import sys
import copy
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def _model_config_dict(config: Dict[str, Any]) -> Dict[str, Any]:
    current_model = config.get("model")
    if isinstance(current_model, dict):
        return dict(current_model)
    if isinstance(current_model, str) and current_model.strip():
        return {"default": current_model.strip()}
    return {}


def _get_credential_pool_strategies(config: Dict[str, Any]) -> Dict[str, str]:
    strategies = config.get("credential_pool_strategies")
    return dict(strategies) if isinstance(strategies, dict) else {}


def _set_credential_pool_strategy(config: Dict[str, Any], provider: str, strategy: str) -> None:
    if not provider:
        return
    strategies = _get_credential_pool_strategies(config)
    strategies[provider] = strategy
    config["credential_pool_strategies"] = strategies


def _supports_same_provider_pool_setup(provider: str) -> bool:
    if not provider or provider == "custom":
        return False
    if provider == "openrouter":
        return True
    from son_of_anton_cli.auth import PROVIDER_REGISTRY

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig:
        return False
    return pconfig.auth_type in {"api_key", "oauth_device_code"}


# Default model lists per provider — used as fallback when the live
# /models endpoint can't be reached.
_DEFAULT_PROVIDER_MODELS = {
    "copilot-acp": [
        "copilot-acp",
    ],
    "copilot": [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5-mini",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-4.1",
        "gpt-4o",
        "gpt-4o-mini",
        "claude-opus-4.6",
        "claude-sonnet-5",
        "claude-sonnet-4.6",
        "claude-sonnet-4.5",
        "claude-haiku-4.5",
        "gemini-2.5-pro",
    ],
    "gemini": [
        "gemini-3.1-pro-preview", "gemini-3-pro-preview",
        "gemini-3.6-flash", "gemini-3.1-flash-lite-preview",
    ],
    "vertex": [
        "google/gemini-3.1-pro-preview", "google/gemini-3-pro-preview",
        "google/gemini-3-flash-preview", "google/gemini-3.1-flash-lite-preview",
        "google/gemini-2.5-pro", "google/gemini-2.5-flash",
    ],
    "zai": ["glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4.5", "glm-4.5-flash"],
    "kimi-coding": ["kimi-k3", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2-turbo-preview"],
    "kimi-coding-cn": ["kimi-k3", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2-turbo-preview"],
    "stepfun": ["step-3.5-flash", "step-3.5-flash-2603"],
    "arcee": ["trinity-large-thinking", "trinity-large-preview", "trinity-mini"],
    "minimax": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
    "minimax-cn": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
    "ai-gateway": ["anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6", "openai/gpt-5", "google/gemini-3-flash"],
    "kilocode": ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6", "openai/gpt-5.4", "google/gemini-3-pro-preview", "google/gemini-3-flash-preview"],
    "opencode-zen": ["x-preview-f-free", "gpt-5.6-sol", "gpt-5.4", "gpt-5.3-codex", "claude-opus-5", "claude-sonnet-5", "gemini-3.7-flash", "glm-5.2", "kimi-k3", "minimax-m3"],
    "opencode-free": ["x-preview-f-free", "hy3-free", "laguna-s-2.1-free", "nemotron-3-ultra-free", "nemotron-3.5-lightning-free", "muse-spark-1.2-contributor-free"],
    "opencode-go": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "gpt-5.6-luna", "grok-4.5", "glm-5.3", "glm-5.2", "mimo-v2.5-pro", "mimo-v2.5", "minimax-m3", "minimax-m2.7", "qwen3.8-max", "qwen3.7-max", "deepseek-v4-pro", "hy3"],
    "huggingface": [
        "Qwen/Qwen3.5-397B-A17B", "Qwen/Qwen3-235B-A22B-Thinking-2507",
        "Qwen/Qwen3-Coder-480B-A35B-Instruct", "deepseek-ai/DeepSeek-R1-0528",
        "deepseek-ai/DeepSeek-V3.2", "moonshotai/Kimi-K2.5",
    ],
}


def _current_reasoning_effort(config: Dict[str, Any]) -> str:
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("reasoning_effort") or "").strip().lower()
    return ""


def _set_reasoning_effort(config: Dict[str, Any], effort: str) -> None:
    agent_cfg = config.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config["agent"] = agent_cfg
    agent_cfg["reasoning_effort"] = effort




# Import config helpers
from son_of_anton_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    get_son_of_anton_home,
    get_config_path,
    get_env_path,
    load_config,
    save_config,
    save_env_value,
    remove_env_value,
    get_env_value,
    ensure_son_of_anton_home,
)
# display_son_of_anton_home imported lazily at call sites (stale-module safety during son-of-anton update)

from son_of_anton_cli.colors import Colors, color


def print_header(title: str):
    """Print a section header."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


from son_of_anton_cli.cli_output import (  # noqa: E402
    print_error,
    print_info,
    print_success,
    print_warning,
)
from son_of_anton_cli.secret_prompt import masked_secret_prompt  # noqa: E402


def is_interactive_stdin() -> bool:
    """Return True when stdin looks like a usable interactive TTY."""
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except Exception:
        return False


def print_noninteractive_setup_guidance(reason: str | None = None) -> None:
    """Print guidance for headless/non-interactive setup flows."""
    print()
    print(color("⚛ Son of Anton Setup — Non-interactive mode", Colors.CYAN, Colors.BOLD))
    print()
    if reason:
        print_info(reason)
    print_info("The interactive wizard cannot be used here.")
    print()
    print_info("Configure Son of Anton using environment variables or config commands:")
    print_info("  son-of-anton config set model.provider custom")
    print_info("  son-of-anton config set model.base_url http://localhost:8080/v1")
    print_info("  son-of-anton config set model.default your-model-name")
    print()
    print_info("Or set OPENAI_API_KEY / DEEPSEEK_API_KEY in your environment.")
    print_info("Run 'son-of-anton setup' in an interactive terminal to use the full wizard.")
    print()


def prompt(question: str, default: str = None, password: bool = False) -> str:
    """Prompt for input with optional default."""
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "

    try:
        if password:
            value = masked_secret_prompt(color(display, Colors.YELLOW))
        else:
            from son_of_anton_cli.cli_output import line_input

            value = line_input(color(display, Colors.YELLOW))

        cleaned = _sanitize_pasted_input(value)
        return cleaned.strip() or default or ""
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)


_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")


def _sanitize_pasted_input(value: str) -> str:
    """Strip terminal bracketed-paste control markers from pasted text."""
    if not isinstance(value, str) or not value:
        return value
    return _BRACKETED_PASTE_PATTERN.sub("", value)


def _curses_prompt_choice(question: str, choices: list, default: int = 0, description: str | None = None) -> int:
    """Single-select menu using curses. Delegates to curses_radiolist."""
    from son_of_anton_cli.curses_ui import curses_radiolist
    return curses_radiolist(question, choices, selected=default, cancel_returns=-1, description=description)



def prompt_choice(question: str, choices: list, default: int = 0, description: str | None = None) -> int:
    """Prompt for a choice from a list with arrow key navigation.

    Escape keeps the current default (skips the question).
    Ctrl+C exits the wizard.
    """
    idx = _curses_prompt_choice(question, choices, default, description=description)
    if idx >= 0:
        if idx == default:
            print_info("  Skipped (keeping current)")
            print()
            return default
        print()
        return idx

    print(color(question, Colors.YELLOW))
    for i, choice in enumerate(choices):
        marker = "●" if i == default else "○"
        if i == default:
            print(color(f"  {marker} {choice}", Colors.GREEN))
        else:
            print(f"  {marker} {choice}")

    print_info(f"  Enter for default ({default + 1})  Ctrl+C to exit")

    while True:
        try:
            value = input(
                color(f"  Select [1-{len(choices)}] ({default + 1}): ", Colors.DIM)
            )
            if not value:
                return default
            idx = int(value) - 1
            if 0 <= idx < len(choices):
                return idx
            print_error(f"Please enter a number between 1 and {len(choices)}")
        except ValueError:
            print_error("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)


def is_noninteractive() -> bool:
    """True when no human is available to answer a prompt.

    The dashboard/desktop spawn CLI actions with ``stdin=DEVNULL`` and
    ``SON_OF_ANTON_NONINTERACTIVE=1`` (see ``son_of_anton_cli/web_server.py``). In that
    context an ``input()`` raises ``EOFError`` immediately, so a prompt that
    aborts on EOF kills the spawned action. Honour the explicit env flag here
    so callers fall back to their default.
    """
    return os.environ.get("SON_OF_ANTON_NONINTERACTIVE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for yes/no. Ctrl+C exits, empty input returns default.

    Non-interactive callers (``SON_OF_ANTON_NONINTERACTIVE=1`` or a closed/redirected
    stdin) have no one to answer, so fall back to ``default`` instead of
    aborting the whole process.
    """
    if is_noninteractive():
        return default

    default_str = "Y/n" if default else "y/N"

    while True:
        try:
            value = (
                input(color(f"{question} [{default_str}]: ", Colors.YELLOW))
                .strip()
                .lower()
            )
        except KeyboardInterrupt:
            print()
            sys.exit(1)
        except EOFError:
            # No stdin to read (closed/redirected, e.g. a spawned action with
            # stdin=DEVNULL). Accept the default rather than exit so the caller
            # can proceed unattended instead of failing the whole command.
            print()
            return default

        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print_error("Please enter 'y' or 'n'")


def prompt_checklist(title: str, items: list, pre_selected: list = None) -> list:
    """
    Display a multi-select checklist and return the indices of selected items.

    Each item in `items` is a display string. `pre_selected` is a list of
    indices that should be checked by default. A "Continue →" option is
    appended at the end — the user toggles items with Space and confirms
    with Enter on "Continue →".

    Falls back to a numbered toggle interface when curses is
    unavailable.

    Returns:
        List of selected indices (not including the Continue option).
    """
    if pre_selected is None:
        pre_selected = []

    from son_of_anton_cli.curses_ui import curses_checklist

    chosen = curses_checklist(
        title,
        items,
        set(pre_selected),
        cancel_returns=set(pre_selected),
    )
    return sorted(chosen)


def _prompt_api_key(var: dict):
    """Display a nicely formatted API key input screen for a single env var."""
    tools = var.get("tools", [])
    tools_str = ", ".join(tools[:3])
    if len(tools) > 3:
        tools_str += f", +{len(tools) - 3} more"

    print()
    print(color(f"  ─── {var.get('description', var['name'])} ───", Colors.CYAN))
    print()
    if tools_str:
        print_info(f"  Enables: {tools_str}")
    if var.get("url"):
        print_info(f"  Get your key at: {var['url']}")
    print()

    if var.get("password"):
        value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
    else:
        value = prompt(f"  {var.get('prompt', var['name'])}")

    if value:
        save_env_value(var["name"], value)
        print_success("  ✓ Saved")
    else:
        print_warning("  Skipped (configure later with 'son-of-anton setup')")


def _print_setup_summary(config: dict, son_of_anton_home):
    """Print the setup completion summary."""
    # Provider readiness — the one thing setup absolutely must produce.
    # Previously a user could cancel the API-key prompt mid-wizard (Enter →
    # "Cancelled."), watch the wizard continue through Terminal/Gateway/Tools,
    # and exit "successfully" with NO working model — believing they were set
    # up. Say so loudly instead (consumer-onboarding audit finding #7).
    try:
        from son_of_anton_cli.auth import resolve_provider

        resolve_provider()
        _provider_ready = True
    except Exception:
        _provider_ready = False
    if not _provider_ready:
        print()
        print_warning("No inference provider is configured — Son of Anton cannot chat yet.")
        print_info("  Finish this one step:")
        print_info("    son-of-anton model            (pick a provider/model)")

    # Tool availability summary
    print()
    print_header("Tool Availability Summary")

    tool_status = []

    # Vision — use the same runtime resolver as the actual vision tools
    try:
        from agent.auxiliary_client import get_available_vision_backends

        _vision_backends = get_available_vision_backends()
    except Exception:
        _vision_backends = []

    if _vision_backends:
        tool_status.append(("Vision (image analysis)", True, None))
    else:
        tool_status.append(("Vision (image analysis)", False, "run 'son-of-anton setup' to configure"))


    # Web tools (Exa, Parallel, Firecrawl, Tavily, or SearXNG)
    web_label = "Web Search & Extract"
    _web_key = next(
        (
            get_env_value(key)
            for key in (
                "EXA_API_KEY",
                "PARALLEL_API_KEY",
                "FIRECRAWL_API_KEY",
                "FIRECRAWL_API_URL",
                "TAVILY_API_KEY",
                "SEARXNG_URL",
            )
            if get_env_value(key)
        ),
        None,
    )
    if _web_key:
        tool_status.append((web_label, True, None))
    else:
        tool_status.append(
            (
                web_label,
                False,
                "EXA_API_KEY, PARALLEL_API_KEY, FIRECRAWL_API_KEY/FIRECRAWL_API_URL, TAVILY_API_KEY, or SEARXNG_URL",
            )
        )

    # Skills Hub
    if get_env_value("GITHUB_TOKEN"):
        tool_status.append(("Skills Hub (GitHub)", True, None))
    else:
        tool_status.append(("Skills Hub (GitHub)", False, "GITHUB_TOKEN"))

    # Terminal (always available if system deps met)
    tool_status.append(("Terminal/Commands", True, None))

    # Task planning (always available, in-memory)
    tool_status.append(("Task Planning (todo)", True, None))

    # Skills (always available -- bundled skills + user-created skills)
    tool_status.append(("Skills (view, create, edit)", True, None))

    # Print status
    available_count = sum(1 for _, avail, _ in tool_status if avail)
    total_count = len(tool_status)

    print_info(f"{available_count}/{total_count} tool categories available:")
    print()

    for name, available, missing_var in tool_status:
        if available:
            print(f"   {color('✓', Colors.GREEN)} {name}")
        else:
            print(
                f"   {color('✗', Colors.RED)} {name} {color(f'(missing {missing_var})', Colors.DIM)}"
            )

    print()

    disabled_tools = [(name, var) for name, avail, var in tool_status if not avail]
    if disabled_tools:
        print_warning(
            "Some tools are disabled. Run 'son-of-anton setup tools' to configure them,"
        )
        from son_of_anton_constants import display_son_of_anton_home as _dhh
        print_warning(f"or edit {_dhh()}/.env directly to add the missing API keys.")
        print()

    # Done banner
    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐", Colors.GREEN
        )
    )
    print(
        color(
            "│              ✓ Setup Complete!                          │", Colors.GREEN
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘", Colors.GREEN
        )
    )
    print()

    # Show file locations prominently
    from son_of_anton_constants import display_son_of_anton_home as _dhh
    print(color(f"📁 All your files are in {_dhh()}/:", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('Settings:', Colors.YELLOW)}  {get_config_path()}")
    print(f"   {color('API Keys:', Colors.YELLOW)}  {get_env_path()}")
    print(
        f"   {color('Data:', Colors.YELLOW)}      {son_of_anton_home}/cron/, sessions/, logs/"
    )
    print()

    print(color("─" * 60, Colors.DIM))
    print()
    print(color("📝 To edit your configuration:", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('son-of-anton setup', Colors.GREEN)}          Re-run the full wizard")
    print(f"   {color('son-of-anton setup model', Colors.GREEN)}    Change model/provider")
    print(f"   {color('son-of-anton setup terminal', Colors.GREEN)} Change terminal backend")
    print(f"   {color('son-of-anton setup gateway', Colors.GREEN)}  Configure messaging")
    print(f"   {color('son-of-anton setup tools', Colors.GREEN)}    Configure tool providers")
    print()
    print(f"   {color('son-of-anton config', Colors.GREEN)}         View current settings")
    print(
        f"   {color('son-of-anton config edit', Colors.GREEN)}    Open config in your editor"
    )
    print(f"   {color('son-of-anton config set <key> <value>', Colors.GREEN)}")
    print("                          Set a specific value")
    print()
    print("   Or edit the files directly:")
    print(f"   {color(f'nano {get_config_path()}', Colors.DIM)}")
    print(f"   {color(f'nano {get_env_path()}', Colors.DIM)}")
    print()

    print(color("─" * 60, Colors.DIM))
    print()
    print(color("🚀 Ready to go!", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('son-of-anton', Colors.GREEN)}              Start chatting")
    print(f"   {color('son-of-anton gateway', Colors.GREEN)}      Start messaging gateway")
    print(f"   {color('son-of-anton doctor', Colors.GREEN)}       Check for issues")
    print()


# Tool categories and provider config are now in tools_config.py (shared
# between `son-of-anton tools` and `son-of-anton setup tools`).


# =============================================================================
# Section 1: Model & Provider Configuration
# =============================================================================



def setup_model_provider(config: dict, *, quick: bool = False):
    """Configure the inference provider and default model.

    Delegates to ``cmd_model()`` (the same flow used by ``son-of-anton model``)
    for provider selection, credential prompting, and model picking.
    This ensures a single code path for all provider setup — any new
    provider added to ``son-of-anton model`` is automatically available here.

    When *quick* is True, skips credential rotation, vision, and TTS
    configuration — used by the streamlined first-time quick setup.
    """
    from son_of_anton_cli.config import load_config, save_config

    print_header("Inference Provider")
    print_info("Choose how to connect to your main chat model.")
    print()

    # Delegate to the shared son-of-anton model flow — handles provider picker,
    # credential prompting, model selection, and config persistence.
    from son_of_anton_cli.main import select_provider_and_model
    try:
        select_provider_and_model()
    except (SystemExit, KeyboardInterrupt):
        print()
        print_info("Provider setup skipped.")
    except Exception as exc:
        logger.debug("select_provider_and_model error during setup: %s", exc)
        print_warning(f"Provider setup encountered an error: {exc}")
        print_info("You can try again later with: son-of-anton model")

    # Re-sync the wizard's config dict from what cmd_model saved to disk.
    # This is critical: cmd_model writes to disk via its own load/save cycle,
    # and the wizard's final save_config(config) must not overwrite those
    # changes with stale values (#4172). Refresh the dict in place so callers
    # that keep the same object see every section the shared model picker may
    # have changed (model, custom_providers, auxiliary, provider metadata, etc.).
    _refreshed = load_config()
    config.clear()
    config.update(_refreshed)

    # Credential rotation and vision-backend selection are no longer prompted
    # here. They have safe defaults (rotation off, vision auto-detected from
    # the main provider) and are configurable on demand via
    # `son-of-anton auth add` and `son-of-anton setup`. This keeps both quick
    # and full setup thin.


    save_config(config)


# =============================================================================
# Section 2: Terminal Backend Configuration
# =============================================================================


def setup_terminal_backend(config: dict):
    """Configure the terminal execution backend."""
    print_header("Terminal Backend")
    print_info("Choose where Son of Anton runs shell commands and code.")
    print_info("This affects tool execution, file access, and isolation.")
    print()

    current_backend = cfg_get(config, "terminal", "backend", default="local")

    # Build backend choices with descriptions
    terminal_choices = [
        "Local - run directly on this machine (default)",
        "SSH - run on a remote machine",
    ]
    idx_to_backend = {0: "local", 1: "ssh"}

    next_idx = 2

    # Add keep current option
    keep_current_idx = next_idx
    terminal_choices.append(f"Keep current ({current_backend})")
    idx_to_backend[keep_current_idx] = current_backend

    terminal_idx = prompt_choice(
        "Select terminal backend:", terminal_choices, keep_current_idx
    )

    selected_backend = idx_to_backend.get(terminal_idx)

    if terminal_idx == keep_current_idx:
        print_info(f"Keeping current backend: {current_backend}")
        return

    config.setdefault("terminal", {})["backend"] = selected_backend

    if selected_backend == "local":
        print_success("Terminal backend: Local")
        print_info("Commands run directly on this machine.")
        # Gateway working directory defaults to home; sudo stays off. Both are
        # configurable later via `son-of-anton setup terminal` / config.yaml.
        config["terminal"].setdefault("cwd", str(Path.home()))

    elif selected_backend == "ssh":
        print_success("Terminal backend: SSH")
        print_info("Run commands on a remote machine via SSH.")

        # SSH host
        current_host = get_env_value("TERMINAL_SSH_HOST") or ""
        host = prompt("  SSH host (hostname or IP)", current_host)
        if host:
            save_env_value("TERMINAL_SSH_HOST", host)

        # SSH user
        current_user = get_env_value("TERMINAL_SSH_USER") or ""
        user = prompt("  SSH user", current_user or os.getenv("USER", ""))
        if user:
            save_env_value("TERMINAL_SSH_USER", user)

        # SSH port
        current_port = get_env_value("TERMINAL_SSH_PORT") or "22"
        port = prompt("  SSH port", current_port)
        if port and port != "22":
            save_env_value("TERMINAL_SSH_PORT", port)

        # SSH key
        current_key = get_env_value("TERMINAL_SSH_KEY") or ""
        default_key = str(Path.home() / ".ssh" / "id_rsa")
        ssh_key = prompt("  SSH private key path", current_key or default_key)
        if ssh_key:
            save_env_value("TERMINAL_SSH_KEY", ssh_key)

        # Test connection
        if host and prompt_yes_no("  Test SSH connection?", True):
            print_info("  Testing connection...")
            import subprocess

            ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
            if ssh_key:
                ssh_cmd.extend(["-i", ssh_key])
            if port and port != "22":
                ssh_cmd.extend(["-p", port])
            ssh_cmd.append(f"{user}@{host}" if user else host)
            ssh_cmd.append("echo ok")
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            if result.returncode == 0:
                print_success("  SSH connection successful!")
            else:
                print_warning(f"  SSH connection failed: {result.stderr.strip()}")
                print_info("  Check your SSH key and host settings.")

    # Sync terminal backend to .env so terminal_tool picks it up directly.
    # config.yaml is the source of truth, but terminal_tool reads TERMINAL_ENV.
    save_env_value("TERMINAL_ENV", selected_backend)
    save_config(config)
    print()
    print_success(f"Terminal backend set to: {selected_backend}")


# =============================================================================
# Section 3: Agent Settings
# =============================================================================


def _apply_default_agent_settings(config: dict):
    """Apply recommended defaults for all agent settings without prompting."""
    config.setdefault("agent", {})["max_turns"] = 150
    # config.yaml is the authoritative source for max_turns; the gateway
    # bridges it into SON_OF_ANTON_MAX_ITERATIONS at startup. We no longer write
    # to .env to avoid the dual-source inconsistency that caused the
    # 60-vs-500 bug (stale .env entry silently shadowing config.yaml).
    remove_env_value("SON_OF_ANTON_MAX_ITERATIONS")

    config.setdefault("display", {})["tool_progress"] = "all"

    config.setdefault("compression", {})["enabled"] = True
    config["compression"]["threshold"] = 0.50

    # Default: never auto-reset sessions. This matches the gateway's own
    # default (SessionResetPolicy.mode = "none"); we still write it
    # explicitly so the choice is visible/editable in config.yaml.
    config.setdefault("session_reset", {})["mode"] = "none"

    save_config(config)
    print_success("Applied recommended defaults:")
    print_info("  Max iterations: 150")
    print_info("  Tool progress: all")
    print_info("  Compression threshold: 0.50")
    print_info("  Session reset: never (use /reset or compression)")
    print_info("  Run `son-of-anton setup agent` later to customize.")


def setup_agent_settings(config: dict):
    """Configure agent behavior: iterations, progress display, compression, session reset."""

    print_header("Agent Settings")
    print()

    # ── Max Iterations ──
    # config.yaml is authoritative; read from there. If a legacy .env
    # entry is still around (from pre-PR#18413 setups), prefer the
    # config value so we don't surface a stale number to the user.
    current_max = str(cfg_get(config, "agent", "max_turns", default=90))
    print_info("Maximum tool-calling iterations per conversation.")
    print_info("Higher = more complex tasks, but costs more tokens.")
    print_info(
        f"Press Enter to keep {current_max}. Use 90 for most tasks or 150+ for open exploration."
    )

    max_iter_str = prompt("Max iterations", current_max)
    try:
        max_iter = int(max_iter_str)
        if max_iter > 0:
            # Write to config.yaml (authoritative) only. Also clean up any
            # stale .env entry from earlier setup runs — the gateway's
            # bridge in gateway/run.py now unconditionally derives
            # SON_OF_ANTON_MAX_ITERATIONS from agent.max_turns at startup.
            config.setdefault("agent", {})["max_turns"] = max_iter
            config.pop("max_turns", None)
            remove_env_value("SON_OF_ANTON_MAX_ITERATIONS")
            print_success(f"Max iterations set to {max_iter}")
    except ValueError:
        print_warning("Invalid number, keeping current value")

    # ── Tool Progress Display ──
    print_info("")
    print_info("Tool Progress Display")
    print_info("Controls how much tool activity is shown (CLI and messaging).")
    print_info("  off     — Silent, just the final response")
    print_info("  new     — Show tool name only when it changes (less noise)")
    print_info("  all     — Show every tool call with a short preview")
    print_info("  verbose — Full args, results, and debug logs")
    print_info("  log     — Silent in chat; write every tool call to ~/.son-of-anton/logs/tool_calls.log (gateway only)")

    current_mode = cfg_get(config, "display", "tool_progress", default="all")
    mode = prompt("Tool progress mode", current_mode)
    if mode.lower() in {"off", "new", "all", "verbose", "log"}:
        if "display" not in config:
            config["display"] = {}
        config["display"]["tool_progress"] = mode.lower()
        save_config(config)
        print_success(f"Tool progress set to: {mode.lower()}")
    else:
        print_warning(f"Unknown mode '{mode}', keeping '{current_mode}'")

    # ── Context Compression ──
    print_header("Context Compression")
    print_info("Automatically summarizes old messages when context gets too long.")
    print_info(
        "Higher threshold = compress later (use more context). Lower = compress sooner."
    )

    config.setdefault("compression", {})["enabled"] = True

    current_threshold = cfg_get(config, "compression", "threshold", default=0.50)
    threshold_str = prompt("Compression threshold (0.5-0.95)", str(current_threshold))
    try:
        threshold = float(threshold_str)
        if 0.5 <= threshold <= 0.95:
            config["compression"]["threshold"] = threshold
    except ValueError:
        pass

    print_success(
        f"Context compression threshold set to {config['compression'].get('threshold', 0.50)}"
    )

    # ── Session Reset Policy ──
    print_header("Session Reset Policy")
    print_info(
        "Messaging sessions (Discord, Slack, etc.) accumulate context over time."
    )
    print_info(
        "Each message adds to the conversation history, which means growing API costs."
    )
    print_info("")
    print_info(
        "To manage this, sessions can automatically reset after a period of inactivity"
    )
    print_info(
        "or at a fixed time each day. When a reset happens, the agent saves important"
    )
    print_info(
        "things to its persistent memory first — but the conversation context is cleared."
    )
    print_info("")
    print_info("You can also manually reset anytime by typing /reset in chat.")
    print_info("")

    reset_choices = [
        "Inactivity + daily reset (reset whichever comes first)",
        "Inactivity only (reset after N minutes of no messages)",
        "Daily only (reset at a fixed hour each day)",
        "Never auto-reset (recommended - context lives until /reset or context compression)",
        "Keep current settings",
    ]

    current_policy = config.get("session_reset", {})
    current_mode = current_policy.get("mode", "none")
    current_idle = current_policy.get("idle_minutes", 1440)
    current_hour = current_policy.get("at_hour", 4)

    default_reset = {"both": 0, "idle": 1, "daily": 2, "none": 3}.get(current_mode, 3)

    reset_idx = prompt_choice("Session reset mode:", reset_choices, default_reset)

    config.setdefault("session_reset", {})

    if reset_idx == 0:  # Both
        config["session_reset"]["mode"] = "both"
        idle_str = prompt("  Inactivity timeout (minutes)", str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config["session_reset"]["idle_minutes"] = idle_val
        except ValueError:
            pass
        hour_str = prompt("  Daily reset hour (0-23, local time)", str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config["session_reset"]["at_hour"] = hour_val
        except ValueError:
            pass
        print_success(
            f"Sessions reset after {config['session_reset'].get('idle_minutes', 1440)} min idle or daily at {config['session_reset'].get('at_hour', 4)}:00"
        )
    elif reset_idx == 1:  # Idle only
        config["session_reset"]["mode"] = "idle"
        idle_str = prompt("  Inactivity timeout (minutes)", str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config["session_reset"]["idle_minutes"] = idle_val
        except ValueError:
            pass
        print_success(
            f"Sessions reset after {config['session_reset'].get('idle_minutes', 1440)} min of inactivity"
        )
    elif reset_idx == 2:  # Daily only
        config["session_reset"]["mode"] = "daily"
        hour_str = prompt("  Daily reset hour (0-23, local time)", str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config["session_reset"]["at_hour"] = hour_val
        except ValueError:
            pass
        print_success(
            f"Sessions reset daily at {config['session_reset'].get('at_hour', 4)}:00"
        )
    elif reset_idx == 3:  # None
        config["session_reset"]["mode"] = "none"
        print_info(
            "Sessions will never auto-reset. Context is managed only by compression."
        )
        print_warning(
            "Long conversations will grow in cost. Use /reset manually when needed."
        )
    # else: keep current (idx == 4)

    save_config(config)


# =============================================================================
# Section 4: Messaging Platforms (Gateway)
# =============================================================================


# _setup_slack and _write_slack_manifest_and_instruct moved to the slack
# plugin: plugins/platforms/slack/adapter.py::interactive_setup (registered
# via setup_fn and dispatched through the plugin path). #41112 / #3823.


def setup_gateway(config: dict):
    """Configure messaging platform integrations."""
    from son_of_anton_cli.gateway import _all_platforms, _platform_status, _configure_platform

    print_header("Messaging Platforms")
    print_info("Connect to messaging platforms to chat with Son of Anton from anywhere.")
    print_info("Toggle with Space, confirm with Enter.")
    print()

    platforms = _all_platforms()

    # Build checklist, pre-selecting already-configured platforms.
    items = []
    pre_selected = []
    for i, plat in enumerate(platforms):
        status = _platform_status(plat)
        items.append(f"{plat['emoji']} {plat['label']}  ({status})")
        if status == "configured":
            pre_selected.append(i)

    selected = prompt_checklist("Select platforms to configure:", items, pre_selected)

    if not selected:
        print_info("No platforms selected. Run 'son-of-anton setup gateway' later to configure.")
    else:
        for idx in selected:
            _configure_platform(platforms[idx])

    # ── Gateway Service Setup ──
    # Count any platform (built-in or plugin) the user configured during this
    # setup pass — reuses ``_platform_status`` so plugin platforms
    # are picked up without another hard-coded env-var list.
    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (
            s == "not configured"
            or s.startswith("partially")
            or s.startswith("plugin disabled")
        )

    any_messaging = any(
        _is_progress(_platform_status(p)) for p in _all_platforms()
    )
    if any_messaging:
        print()
        print_info("━" * 50)
        print_success("Messaging platforms configured!")

        # Check if any home channels are missing
        missing_home = []
        if get_env_value("DISCORD_BOT_TOKEN") and not get_env_value(
            "DISCORD_HOME_CHANNEL"
        ):
            missing_home.append("Discord")
        if get_env_value("SLACK_BOT_TOKEN") and not get_env_value("SLACK_HOME_CHANNEL"):
            missing_home.append("Slack")

        if missing_home:
            print()
            print_warning(f"No home channel set for: {', '.join(missing_home)}")
            print_info("   Without a home channel, cron jobs and cross-platform")
            print_info("   messages can't be delivered to those platforms.")
            print_info("   Set one later with /set-home in your chat, or:")
            for plat in missing_home:
                print_info(
                    f"     son-of-anton config set {plat.upper()}_HOME_CHANNEL <channel_id>"
                )

    # ── Gateway Service Setup ──
    # Runs UNCONDITIONALLY — even with zero platforms configured. A gateway
    # without platforms is a supported mode (cron scheduler keeps running,
    # and adapters come up automatically once tokens are added later, e.g.
    # via `son-of-anton import` or `son-of-anton setup gateway`). Gating this on
    # messaging config was the bug that left install-then-import machines
    # with registered cron jobs and restored bot tokens but no process to
    # serve them.
    from son_of_anton_cli.gateway import (
        _is_service_running,
        supports_systemd_services,
        ensure_gateway_service,
        systemd_restart,
        launchd_restart,
        UserSystemdUnavailableError,
        SystemScopeRequiresRootError,
        _system_scope_wizard_would_need_root,
        _print_system_scope_remediation,
    )
    import platform as _platform

    _is_macos = _platform.system() == "Darwin"
    supports_systemd = supports_systemd_services()

    print()
    if _is_service_running():
        # Already running: only offer a restart when this setup pass may
        # have changed platform config — a restart interrupts any active
        # session, so it stays behind a prompt.
        if supports_systemd and _system_scope_wizard_would_need_root():
            _print_system_scope_remediation("restart")
        elif any_messaging and prompt_yes_no(
            "  Restart the gateway to pick up changes?", True
        ):
            try:
                if supports_systemd:
                    systemd_restart()
                elif _is_macos:
                    launchd_restart()
            except UserSystemdUnavailableError as e:
                print_error("  Restart failed — user systemd not reachable:")
                for line in str(e).splitlines():
                    print(f"  {line}")
            except SystemScopeRequiresRootError as e:
                # Defense in depth: the pre-check above should have
                # caught this, but a race (unit file appearing mid-run)
                # could still land here. Previously this exited the
                # whole wizard via sys.exit(1).
                print_error(f"  Restart failed: {e}")
                _print_system_scope_remediation("restart")
            except Exception as e:
                print_error(f"  Restart failed: {e}")
    else:
        # Not running: install (if needed) and start, no questions asked.
        ensure_gateway_service(context="setup")

    print_info("━" * 50)


# =============================================================================
# Section 5: Tool Configuration (delegates to unified tools_config.py)
# =============================================================================


def setup_tools(config: dict, first_install: bool = False):
    """Configure tools — delegates to the unified tools_command() in tools_config.py.

    Both `son-of-anton setup tools` and `son-of-anton tools` use the same flow:
    platform selection → toolset toggles → provider/API key configuration.

    Args:
        first_install: When True, uses the simplified first-install flow
            (no platform menu, prompts for all unconfigured API keys).
    """
    from son_of_anton_cli.tools_config import tools_command

    tools_command(first_install=first_install, config=config)


# =============================================================================
# Shared Metrics
# =============================================================================


def setup_telemetry(config: dict):
    """Configure the local, privacy-safe shared-metrics subscriber."""
    print_header("Shared Metrics")
    print_info("Shared metrics contain only bounded counters and histograms.")
    print_info("Packages stay under this Son of Anton profile and are not uploaded.")

    telemetry = config.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
        config["telemetry"] = telemetry
    shared_metrics = telemetry.get("shared_metrics")
    if not isinstance(shared_metrics, dict):
        shared_metrics = {}
        telemetry["shared_metrics"] = shared_metrics

    current = shared_metrics.get("enabled") is True
    shared_metrics["enabled"] = prompt_yes_no(
        "Enable local shared metrics?",
        default=current,
    )
    if shared_metrics["enabled"]:
        print_success("Local shared metrics enabled.")
    else:
        print_info("Local shared metrics disabled.")


# =============================================================================
# Post-Migration Section Skip Logic
# =============================================================================

def _model_section_has_credentials(config: dict) -> bool:
    """Return True when any known inference provider has usable credentials.

    Sources of truth:
      * ``PROVIDER_REGISTRY`` in ``son_of_anton_cli.auth`` — lists every supported
        provider along with its ``api_key_env_vars``.
      * ``active_provider`` in the auth store — covers non-api-key auth flows.
      * ``OPENAI_API_KEY`` — the generic OpenAI-compatible fallback.
    """
    try:
        from son_of_anton_cli.auth import get_active_provider
        if get_active_provider():
            return True
    except Exception:
        pass

    try:
        from son_of_anton_cli.auth import PROVIDER_REGISTRY
    except Exception:
        PROVIDER_REGISTRY = {}  # type: ignore[assignment]

    def _has_key(pconfig) -> bool:
        for env_var in pconfig.api_key_env_vars:
            if get_env_value(env_var):
                return True
        return False

    # Prefer the provider declared in config.yaml, avoids false positives
    # from stray env vars (GH_TOKEN, etc.) when the user has already picked
    # a different provider.
    model_cfg = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_cfg, dict):
        provider_id = (model_cfg.get("provider") or "").strip().lower()
        if provider_id in PROVIDER_REGISTRY:
            if _has_key(PROVIDER_REGISTRY[provider_id]):
                return True

    # Generic OpenAI-compatible fallback (no provider declared in config).
    if get_env_value("OPENAI_API_KEY"):
        return True

    for pid, pconfig in PROVIDER_REGISTRY.items():
        if _has_key(pconfig):
            return True
    return False


def _gateway_platform_short_label(label: str) -> str:
    """Strip trailing parenthetical qualifiers from a gateway platform label."""
    base = label.split("(", 1)[0].strip()
    return base or label


def _get_section_config_summary(config: dict, section_key: str) -> Optional[str]:
    """Return a short summary if a setup section is already configured, else None.

    Used after OpenClaw migration to detect which sections can be skipped.
    ``get_env_value`` is the module-level import from son_of_anton_cli.config
    so that test patches on ``setup_mod.get_env_value`` take effect.
    """
    if section_key == "model":
        if not _model_section_has_credentials(config):
            return None
        model = config.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        if isinstance(model, dict):
            return str(model.get("default") or model.get("model") or "configured")
        return "configured"

    elif section_key == "terminal":
        backend = cfg_get(config, "terminal", "backend", default="local")
        return f"backend: {backend}"

    elif section_key == "agent":
        max_turns = cfg_get(config, "agent", "max_turns", default=90)
        return f"max turns: {max_turns}"

    elif section_key == "gateway":
        from son_of_anton_cli.gateway import _all_platforms, _platform_status
        # Count any non-empty status other than the "not configured" sentinel —
        # platforms like Signal ("partially configured") indicate the user
        # has already started setup and we shouldn't force the section to rerun.
        configured = [
            _gateway_platform_short_label(plat["label"])
            for plat in _all_platforms()
            if _platform_status(plat) and _platform_status(plat) != "not configured"
        ]
        if configured:
            return ", ".join(configured)
        return None  # No platforms configured — section must run

    elif section_key == "tools":
        tools = []
        if get_env_value("ELEVENLABS_API_KEY"):
            tools.append("TTS/ElevenLabs")
        if get_env_value("BROWSERBASE_API_KEY"):
            tools.append("Browser")
        if get_env_value("FIRECRAWL_API_KEY"):
            tools.append("Firecrawl")
        if tools:
            return ", ".join(tools)
        return None

    return None


def _skip_configured_section(
    config: dict, section_key: str, label: str
) -> bool:
    """Show an already-configured section summary and offer to skip.

    Returns True if the user chose to skip, False if the section should run.
    """
    summary = _get_section_config_summary(config, section_key)
    if not summary:
        return False
    print()
    print_success(f"  {label}: {summary}")
    return not prompt_yes_no(f"  Reconfigure {label.lower()}?", default=False)


# =============================================================================
# OpenClaw Migration
# =============================================================================


_OPENCLAW_SCRIPT = (
    get_son_of_anton_home()
    / "skills"
    / "migration"
    / "openclaw-migration"
    / "scripts"
    / "openclaw_to_son_of_anton.py"
)


def _load_openclaw_migration_module():
    """Load the openclaw_to_son_of_anton migration script as a module.

    Returns the loaded module, or None if the script can't be loaded.
    """
    if not _OPENCLAW_SCRIPT.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        "openclaw_to_son_of_anton", _OPENCLAW_SCRIPT
    )
    if spec is None or spec.loader is None:
        return None

    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules so @dataclass can resolve the module
    # (Python 3.11+ requires this for dynamically loaded modules)
    import sys as _sys
    _sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(spec.name, None)
        raise
    return mod


# Item kinds that represent high-impact changes warranting explicit warnings.
# Gateway tokens/channels can hijack messaging platforms from the old agent.
# Config values may have different semantics between OpenClaw and Son of Anton.
# Instruction/context files (.md) can contain incompatible setup procedures.
_HIGH_IMPACT_KIND_KEYWORDS = {
    "gateway": "⚠ Gateway/messaging — this will configure Son of Anton to use your OpenClaw messaging channels",
    "slack": "⚠ Slack — this will point Son of Anton at your OpenClaw Slack workspace",
    "discord": "⚠ Discord — this will point Son of Anton at your OpenClaw Discord bot",
    "config": "⚠ Config values — OpenClaw settings may not map 1:1 to Son of Anton equivalents",
    "soul": "⚠ Instruction file — may contain OpenClaw-specific setup/restart procedures",
    "memory": "⚠ Memory/context file — may reference OpenClaw-specific infrastructure",
    "context": "⚠ Context file — may contain OpenClaw-specific instructions",
}


def _print_migration_preview(report: dict):
    """Print a detailed dry-run preview of what migration would do.

    Groups items by category and adds explicit warnings for high-impact
    changes like gateway token takeover and config value differences.
    """
    items = report.get("items", [])
    if not items:
        print_info("Nothing to migrate.")
        return

    migrated_items = [i for i in items if i.get("status") == "migrated"]
    conflict_items = [i for i in items if i.get("status") == "conflict"]
    skipped_items = [i for i in items if i.get("status") == "skipped"]

    warnings_shown = set()

    if migrated_items:
        print(color("  Would import:", Colors.GREEN))
        for item in migrated_items:
            kind = item.get("kind", "unknown")
            dest = item.get("destination", "")
            if dest:
                dest_short = str(dest).replace(str(Path.home()), "~")
                print(f"      {kind:<22s} → {dest_short}")
            else:
                print(f"      {kind}")

            # Check for high-impact items and collect warnings
            kind_lower = kind.lower()
            dest_lower = str(dest).lower()
            for keyword, warning in _HIGH_IMPACT_KIND_KEYWORDS.items():
                if keyword in kind_lower or keyword in dest_lower:
                    warnings_shown.add(warning)
        print()

    if conflict_items:
        print(color("  Would overwrite (conflicts with existing Son of Anton config):", Colors.YELLOW))
        for item in conflict_items:
            kind = item.get("kind", "unknown")
            reason = item.get("reason", "already exists")
            print(f"      {kind:<22s}  {reason}")
        print()

    if skipped_items:
        print(color("  Would skip:", Colors.DIM))
        for item in skipped_items:
            kind = item.get("kind", "unknown")
            reason = item.get("reason", "")
            print(f"      {kind:<22s}  {reason}")
        print()

    # Print collected warnings
    if warnings_shown:
        print(color("  ── Warnings ──", Colors.YELLOW))
        for warning in sorted(warnings_shown):
            print(color(f"    {warning}", Colors.YELLOW))
        print()
        print(color("  Note: OpenClaw config values may have different semantics in Son of Anton.", Colors.YELLOW))
        print(color("  For example, OpenClaw's tool_call_execution: \"auto\" ≠ Son of Anton's yolo mode.", Colors.YELLOW))
        print(color("  Instruction files (.md) from OpenClaw may contain incompatible procedures.", Colors.YELLOW))
        print()


def _offer_openclaw_migration(son_of_anton_home: Path) -> bool:
    """Detect ~/.openclaw and offer to migrate during first-time setup.

    Runs a dry-run first to show the user exactly what would be imported,
    overwritten, or taken over. Only executes after explicit confirmation.

    Returns True if migration ran successfully, False otherwise.
    """
    openclaw_dir = Path.home() / ".openclaw"
    if not openclaw_dir.is_dir():
        return False

    if not _OPENCLAW_SCRIPT.exists():
        return False

    print()
    print_header("OpenClaw Installation Detected")
    print_info(f"Found OpenClaw data at {openclaw_dir}")
    print_info("Son of Anton can preview what would be imported before making any changes.")
    print()

    if not prompt_yes_no("Would you like to see what can be imported?", default=True):
        print_info(
            "Skipping migration. You can run it later with: son-of-anton claw migrate --dry-run"
        )
        return False

    # Ensure config.yaml exists before migration tries to read it
    config_path = get_config_path()
    if not config_path.exists():
        save_config(load_config())

    # Load the migration module
    try:
        mod = _load_openclaw_migration_module()
        if mod is None:
            print_warning("Could not load migration script.")
            return False
    except Exception as e:
        print_warning(f"Could not load migration script: {e}")
        logger.debug("OpenClaw migration module load error", exc_info=True)
        return False

    # ── Phase 1: Dry-run preview ──
    try:
        selected = mod.resolve_selected_options(None, None, preset="full")
        dry_migrator = mod.Migrator(
            source_root=openclaw_dir.resolve(),
            target_root=son_of_anton_home.resolve(),
            execute=False,  # dry-run — no files modified
            workspace_target=None,
            overwrite=True,  # show everything including conflicts
            migrate_secrets=True,
            output_dir=None,
            selected_options=selected,
            preset_name="full",
        )
        preview_report = dry_migrator.migrate()
    except Exception as e:
        print_warning(f"Migration preview failed: {e}")
        logger.debug("OpenClaw migration preview error", exc_info=True)
        return False

    # Display the full preview
    preview_summary = preview_report.get("summary", {})
    preview_count = preview_summary.get("migrated", 0)

    if preview_count == 0:
        print()
        print_info("Nothing to import from OpenClaw.")
        return False

    print()
    print_header(f"Migration Preview — {preview_count} item(s) would be imported")
    print_info("No changes have been made yet. Review the list below:")
    print()
    _print_migration_preview(preview_report)

    # ── Phase 2: Confirm and execute ──
    if not prompt_yes_no("Proceed with migration?", default=False):
        print_info(
            "Migration cancelled. You can run it later with: son-of-anton claw migrate"
        )
        print_info(
            "Use --dry-run to preview again, or --preset minimal for a lighter import."
        )
        return False

    # Execute the migration — overwrite=False so existing Son of Anton configs are
    # preserved. The user saw the preview; conflicts are skipped by default.
    try:
        migrator = mod.Migrator(
            source_root=openclaw_dir.resolve(),
            target_root=son_of_anton_home.resolve(),
            execute=True,
            workspace_target=None,
            overwrite=False,  # preserve existing Son of Anton config
            migrate_secrets=True,
            output_dir=None,
            selected_options=selected,
            preset_name="full",
        )
        report = migrator.migrate()
    except Exception as e:
        print_warning(f"Migration failed: {e}")
        logger.debug("OpenClaw migration error", exc_info=True)
        return False

    # Print final summary
    summary = report.get("summary", {})
    migrated = summary.get("migrated", 0)
    skipped = summary.get("skipped", 0)
    conflicts = summary.get("conflict", 0)
    errors = summary.get("error", 0)

    print()
    if migrated:
        print_success(f"Imported {migrated} item(s) from OpenClaw.")
    if conflicts:
        print_info(f"Skipped {conflicts} item(s) that already exist in Son of Anton (use son-of-anton claw migrate --overwrite to force).")
    if skipped:
        print_info(f"Skipped {skipped} item(s) (not found or unchanged).")
    if errors:
        print_warning(f"{errors} item(s) had errors — check the migration report.")

    output_dir = report.get("output_dir")
    if output_dir:
        print_info(f"Full report saved to: {output_dir}")

    print_success("Migration complete! Continuing with setup...")
    return True


# =============================================================================
# Main Wizard Orchestrator
# =============================================================================

SETUP_SECTIONS = [
    ("model", "Model & Provider", setup_model_provider),
    ("terminal", "Terminal Backend", setup_terminal_backend),
    ("gateway", "Messaging Platforms (Gateway)", setup_gateway),
    ("tools", "Tools", setup_tools),
    ("telemetry", "Shared Metrics", setup_telemetry),
    ("agent", "Agent Settings", setup_agent_settings),
]
def run_setup_wizard(args):
    """Run the interactive setup wizard.

    Supports full, quick, and section-specific setup:
      son-of-anton setup           — full or quick (auto-detected)
      son-of-anton setup model     — just model/provider
      son-of-anton setup terminal  — just terminal backend
      son-of-anton setup gateway   — just messaging platforms
      son-of-anton setup tools     — just tool configuration
      son-of-anton setup telemetry — just local shared metrics
      son-of-anton setup agent     — just agent settings
    """
    from son_of_anton_cli.config import is_managed, managed_error
    if is_managed():
        managed_error("run setup wizard")
        return
    ensure_son_of_anton_home()

    reset_requested = bool(getattr(args, "reset", False))
    if reset_requested:
        save_config(copy.deepcopy(DEFAULT_CONFIG))
        print_success("Configuration reset to defaults.")

    reconfigure_requested = bool(getattr(args, "reconfigure", False))
    quick_requested = bool(getattr(args, "quick", False))

    config = load_config()
    son_of_anton_home = get_son_of_anton_home()

    # Back up existing config before setup modifies it (#3522)
    config_path = get_config_path()
    if config_path.exists():
        from datetime import datetime as _dt
        _backup_path = config_path.with_suffix(
            f".yaml.bak.{_dt.now().strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            import shutil
            shutil.copy2(config_path, _backup_path)
        except Exception:
            _backup_path = None
    else:
        _backup_path = None

    # Detect non-interactive environments (headless SSH, CI/CD)
    non_interactive = getattr(args, 'non_interactive', False)
    if not non_interactive and not is_interactive_stdin():
        non_interactive = True

    if non_interactive:
        print_noninteractive_setup_guidance(
            "Running in a non-interactive environment (no TTY detected)."
        )
        return

    # Check if a specific section was requested
    section = getattr(args, "section", None)
    if section:
        for key, label, func in SETUP_SECTIONS:
            if key == section:
                print()
                print(
                    color(
                        "┌─────────────────────────────────────────────────────────┐",
                        Colors.MAGENTA,
                    )
                )
                print(color(f"│     ⚛ Son of Anton Setup — {label:<34s} │", Colors.MAGENTA))
                print(
                    color(
                        "└─────────────────────────────────────────────────────────┘",
                        Colors.MAGENTA,
                    )
                )
                func(config)
                save_config(config)
                print()
                print_success(f"{label} configuration complete!")
                return

        print_error(f"Unknown setup section: {section}")
        print_info(f"Available sections: {', '.join(k for k, _, _ in SETUP_SECTIONS)}")
        return

    # Check if this is an existing installation with a provider configured
    from son_of_anton_cli.auth import get_active_provider

    active_provider = get_active_provider()
    is_existing = (
        bool(get_env_value("OPENAI_API_KEY"))
        or bool(get_env_value("OPENAI_BASE_URL"))
        or active_provider is not None
    )

    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│             ⚛ Son of Anton Agent Setup Wizard                │", Colors.MAGENTA
        )
    )
    print(
        color(
            "├─────────────────────────────────────────────────────────┤",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│  Let's configure your Son of Anton Agent installation.       │", Colors.MAGENTA
        )
    )
    print(
        color(
            "│  Press Ctrl+C at any time to exit.                     │", Colors.MAGENTA
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘",
            Colors.MAGENTA,
        )
    )

    migration_ran = False

    if is_existing:
        # Existing install — default is the full-wizard reconfigure flow.
        # Every prompt shows the current value as its default, so pressing
        # Enter keeps it.  Opt into `--quick` for the narrow "just fill in
        # missing items" flow (useful after a partial OpenClaw migration
        # or when a required API key got cleared).
        if quick_requested:
            _run_quick_setup(config, son_of_anton_home)
            return

        print()
        print_header("Reconfigure")
        print_success("You already have Son of Anton configured.")
        print_info("Running the full wizard — each prompt shows your current value.")
        print_info("Press Enter to keep it, or type a new value to change it.")
        print_info("")
        print_info("Tip: jump straight to a section with 'son-of-anton setup model|terminal|")
        print_info("     gateway|tools|agent', or fill only missing items with --quick.")
        # Fall through to the "Full Setup — run all sections" block below.
        # --reconfigure is now the default on existing installs; the flag
        # is preserved for backwards compatibility but is a no-op here.
    else:
        # ── First-Time Setup ──
        print()

        # --reconfigure / --quick on a fresh install are meaningless — fall
        # through to the normal first-time flow.
        if reconfigure_requested or quick_requested:
            print_info("No existing configuration found — running first-time setup.")
            print()

        # Offer OpenClaw migration before configuration begins
        migration_ran = _offer_openclaw_migration(son_of_anton_home)
        if migration_ran:
            config = load_config()

        setup_mode = prompt_choice(
            "How would you like to set up Son of Anton?",
            [
                "Quick Setup — model + provider, terminal & messaging (recommended)",
                "Full setup — configure every provider, tool & option yourself (bring your own keys)",
                "Blank Slate — everything off except the bare minimum; opt in to each capability",
            ],
            0,
        )

        if setup_mode == 0:
            _run_first_time_quick_setup(config, son_of_anton_home, is_existing)
            return
        if setup_mode == 2:
            _run_blank_slate_setup(config, son_of_anton_home, is_existing)
            return

    # ── Full Setup — run all sections ──
    print_header("Configuration Location")
    print_info(f"Config file:  {get_config_path()}")
    print_info(f"Secrets file: {get_env_path()}")
    print_info(f"Data folder:  {son_of_anton_home}")
    print_info(f"Install dir:  {PROJECT_ROOT}")
    print()
    print_info("You can edit these files directly or use 'son-of-anton config edit'")

    if migration_ran:
        print()
        print_info("Settings were imported from OpenClaw.")
        print_info("Each section below will show what was imported — press Enter to keep,")
        print_info("or choose to reconfigure if needed.")

    # Section 1: Model & Provider
    if not (migration_ran and _skip_configured_section(config, "model", "Model & Provider")):
        setup_model_provider(config)

    # Section 2: Terminal Backend
    if not (migration_ran and _skip_configured_section(config, "terminal", "Terminal Backend")):
        setup_terminal_backend(config)

    # Section 3: Agent Settings — no longer prompted. First installs get the
    # recommended defaults silently; existing installs keep whatever they have.
    # Tune later with `son-of-anton setup agent`.
    if not is_existing:
        _apply_default_agent_settings(config)

    # Section 4: Messaging Platforms
    if not (migration_ran and _skip_configured_section(config, "gateway", "Messaging Platforms")):
        setup_gateway(config)
    else:
        # Section skipped (migrated config) — still make sure the gateway
        # service exists so cron jobs and migrated platforms actually run.
        from son_of_anton_cli.gateway import ensure_gateway_service
        ensure_gateway_service(context="setup")

    # Section 5: Tools
    if not (migration_ran and _skip_configured_section(config, "tools", "Tools")):
        setup_tools(config, first_install=not is_existing)

    # Save and show summary
    save_config(config)
    if _backup_path and _backup_path.exists():
        print_info(f"Previous config backed up to: {_backup_path}")
        print_info("If setup changed a value you customized, restore it with:")
        print_info(f"  cp {_backup_path} {config_path}")
    _print_setup_summary(config, son_of_anton_home)


def _run_first_time_quick_setup(config: dict, son_of_anton_home, is_existing: bool):
    """Streamlined first-time setup: model/provider, terminal & messaging.

    Runs the standard provider + model selection (custom endpoint or API-key
    provider), then configures the terminal backend and (optionally) a
    messaging platform. Applies sensible defaults for everything else (agent
    settings, tools); the user can customize later via ``son-of-anton setup
    <section>`` or switch providers with ``son-of-anton model``.
    """
    from son_of_anton_cli.config import load_config

    # Step 1: Model & Provider — the standard selection flow.
    print()
    print_header("Inference Provider")
    print_info("Choose how to connect to your main chat model.")
    print_info("  DeepSeek / OpenAI API keys, or a custom endpoint (llama-swap, ollama, ...).")
    print()
    try:
        from son_of_anton_cli.main import select_provider_and_model

        select_provider_and_model()
    except (KeyboardInterrupt, EOFError):
        print()
        print_info("Provider setup cancelled.")
    except Exception as exc:
        logger.debug("select_provider_and_model error during quick setup: %s", exc)
        print_warning(f"Provider setup encountered an error: {exc}")
        print_info("You can try again later with: son-of-anton model")

    # Re-sync the wizard's config dict from disk — the provider flow writes
    # via its own load/save cycle, and the wizard's later save_config(config)
    # must not clobber those values (#4172).
    _refreshed = load_config()
    config.clear()
    config.update(_refreshed)

    # Step 2: Terminal Backend — where commands run is a core decision
    setup_terminal_backend(config)

    # Step 3: Apply defaults for everything else
    _apply_default_agent_settings(config)

    save_config(config)

    # Step 4: Offer messaging gateway setup
    print()
    gateway_choice = prompt_choice(
        "Connect a messaging platform? (Discord, Slack, Signal, etc.)",
        [
            "Set up messaging now (recommended)",
            "Skip — set up later with 'son-of-anton setup gateway'",
        ],
        0,
    )

    if gateway_choice == 0:
        setup_gateway(config)
        save_config(config)
    else:
        # Messaging skipped — still install/start the gateway service so cron
        # jobs run and platforms come alive as soon as tokens are added later
        # (e.g. via `son-of-anton import` from another machine).
        from son_of_anton_cli.gateway import ensure_gateway_service
        ensure_gateway_service(context="setup")

    print()
    print_success("Setup complete! You're ready to go.")
    print()
    print_info("  Configure all settings:    son-of-anton setup")
    if gateway_choice != 0:
        print_info("  Connect Discord/Slack:  son-of-anton setup gateway")
    print()

    _print_setup_summary(config, son_of_anton_home)


def _blank_slate_minimal_toolsets(config: dict):
    """Write the minimal toolset state for a Blank Slate install.

    Only ``file`` and ``terminal`` are enabled. Two layers enforce this:

    1. ``platform_toolsets["cli"] = ["file", "terminal"]`` — an explicit list of
       configurable keys, which the resolver treats as authoritative
       (``has_explicit_config``) so default toolsets aren't re-expanded.
    2. ``agent.disabled_toolsets`` — a global hard-suppression list (applied last
       in ``_get_platform_tools``, overriding every other path including the
       non-configurable platform-toolset recovery that would otherwise re-add
       toolsets like ``kanban``). We list every known toolset except the two we
       keep, guaranteeing a true blank slate regardless of platform/recovery
       quirks. The user re-enables any of them later via ``son-of-anton tools`` (which
       rewrites ``platform_toolsets``) or by editing ``agent.disabled_toolsets``.
    """
    keep = {"file", "terminal"}
    config.setdefault("platform_toolsets", {})["cli"] = sorted(keep)

    try:
        from toolsets import TOOLSETS
        from son_of_anton_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_plugin_toolset_keys

        all_keys = set()
        all_keys.update(k for k, _, _ in CONFIGURABLE_TOOLSETS)
        all_keys.update(_get_plugin_toolset_keys())
        # Plain (non-composite) TOOLSETS entries — catches recovered toolsets
        # like ``kanban`` that aren't in CONFIGURABLE_TOOLSETS but get re-added.
        for k, tdef in TOOLSETS.items():
            if k.startswith("son-of-anton-"):
                continue  # platform composites — not user-facing toolsets
            if isinstance(tdef, dict) and tdef.get("includes"):
                continue  # composite groupings, not leaf toolsets
            if isinstance(tdef, dict) and tdef.get("posture"):
                continue  # posture toolsets (e.g. coding) are session-level
                # selections made by agent/coding_context.py — not permanent
                # user-facing disables. Adding them here causes model_tools
                # to subtract their tools (terminal, read_file, …) from the
                # minimal Blank Slate surface (#57315).
            all_keys.add(k)

        disabled = sorted(all_keys - keep)
        if disabled:
            config.setdefault("agent", {})["disabled_toolsets"] = disabled
    except Exception as exc:
        logger.debug("blank-slate disabled_toolsets computation skipped: %s", exc)


def _blank_slate_minimize_config(config: dict):
    """Turn OFF the optional config features for a Blank Slate install.

    Everything here is opt-in afterwards via ``son-of-anton setup agent`` /
    ``son-of-anton config set``. We keep only what's needed to run.
    """
    config.setdefault("agent", {})["max_turns"] = 90

    # Compression off — minimal footprint; user opts in if they want long sessions.
    config.setdefault("compression", {})["enabled"] = False

    # No automatic memory / user-profile capture.
    mem = config.setdefault("memory", {})
    mem["memory_enabled"] = False
    mem["user_profile_enabled"] = False

    # No filesystem checkpoints, no smart model routing, no auto session reset.
    config.setdefault("checkpoints", {})["enabled"] = False
    config.setdefault("smart_model_routing", {})["enabled"] = False
    config.setdefault("session_reset", {})["mode"] = "none"

    # Quiet, minimal display.
    config.setdefault("display", {})["tool_progress"] = "all"


def _run_blank_slate_setup(config: dict, son_of_anton_home, is_existing: bool):
    """Blank Slate setup — start with everything off except the bare minimum.

    Forces only the essentials to run an agent (provider + model, the file and
    terminal toolsets) and turns every other tool/skill/plugin/MCP/config
    feature OFF. After applying that minimal baseline, the user chooses one of
    two paths:

      1. Start with everything disabled — finish now with the minimal agent.
      2. Walk through every configuration — opt each capability back in.

    Either way nothing is enabled that the user did not explicitly choose.
    """

    print()
    print_header("Blank Slate Setup")
    print_info("Everything starts OFF. First we force-enable only what's required")
    print_info("to run an agent, then you choose whether to stop there or walk")
    print_info("through enabling more — opting in to exactly what you want.")
    print_info("")
    print_info("Forced on: Provider & Model, File Operations, Terminal.")
    print_info("Everything else (web, browser, code exec, vision, memory,")
    print_info("delegation, cron, skills, plugins, MCP, …) starts disabled.")
    print()

    # ── Step 1: Provider & Model (REQUIRED — the agent cannot run without it) ──
    print_header("Step 1 — Provider & Model (required)")
    setup_model_provider(config)
    save_config(config)

    # ── Step 2: Terminal backend (where commands run — a core decision) ──
    print_header("Step 2 — Terminal Backend")
    setup_terminal_backend(config)

    # ── Step 3: Lock in the minimal toolset + minimized config knobs ──
    _blank_slate_minimal_toolsets(config)
    _blank_slate_minimize_config(config)
    save_config(config)
    print()
    print_success("Minimal baseline applied:")
    print_info("  Toolsets: file, terminal (everything else off)")
    print_info("  Compression, memory, checkpoints, smart routing: off")

    # ── The fork: stop here, or walk through enabling things ──
    print()
    print_header("How far do you want to go?")
    path = prompt_choice(
        "Your minimal agent is ready. What next?",
        [
            "Start with everything disabled — finish now (most minimal)",
            "Walk through all configurations — opt in to tools, skills, plugins, MCP",
        ],
        0,
    )

    if path == 0:
        save_config(config)
        # Blank Slate means no bundled skills; record the opt-out so future
        # `son-of-anton update` runs don't re-inject them.
        try:
            from tools.skills_sync import set_bundled_skills_opt_out
            set_bundled_skills_opt_out(True)
        except Exception as exc:
            logger.debug("blank-slate skill opt-out error: %s", exc)
        print()
        print_success("Blank Slate setup complete — minimal agent ready.")
        print_info("Enable anything later, on demand:")
        print_info("  Enable tools:        son-of-anton tools")
        print_info("  Seed skills:         son-of-anton skills opt-in --sync")
        print_info("  Add MCP servers:     son-of-anton mcp add")
        print_info("  Enable plugins:      son-of-anton plugins")
        print_info("  Tune agent settings: son-of-anton setup agent")
        print()
        _print_setup_summary(config, son_of_anton_home)
        return

    # ── Walkthrough path — opt in to each capability ──
    _blank_slate_walkthrough(config, son_of_anton_home)


def _blank_slate_walkthrough(config: dict, son_of_anton_home):
    """Opt-in walkthrough for Blank Slate: skills, tools, plugins, MCP, gateway."""
    from son_of_anton_cli.config import load_config

    # ── Bundled skills — default to NONE, offer to seed all ──
    print()
    print_header("Bundled Skills")
    print_info("Blank Slate ships with NO bundled skills by default.")
    seed_skills = prompt_yes_no(
        "Seed the full bundled skill catalog? (No = start with zero skills)",
        default=False,
    )
    try:
        from tools.skills_sync import set_bundled_skills_opt_out, sync_skills
        if seed_skills:
            # Make sure no stale opt-out marker blocks the seed, then sync.
            set_bundled_skills_opt_out(False)
            result = sync_skills(quiet=True)
            copied = len(result.get("copied", [])) if isinstance(result, dict) else 0
            print_success(f"Seeded {copied} bundled skills.")
        else:
            set_bundled_skills_opt_out(True)
            print_info("No skills seeded. A .no-bundled-skills marker keeps future")
            print_info("`son-of-anton update` runs from re-injecting them. Opt back in any")
            print_info("time with `son-of-anton skills opt-in --sync`.")
    except Exception as exc:
        logger.debug("blank-slate skill handling error: %s", exc)
        print_warning(f"Skill setup step encountered an error: {exc}")

    # ── Walk through enabling additional tools ──
    print()
    print_header("Tools")
    print_info("Pick exactly which additional toolsets to turn on.")
    print_info("(file and terminal are already on; leave the rest off if you want")
    print_info(" the most minimal agent.)")
    if prompt_yes_no("Open the tool selector to enable more tools?", default=False):
        try:
            from son_of_anton_cli.tools_config import tools_command
            tools_command(first_install=False, config=config)
            # tools_command saves via its own load/save cycle — re-sync.
            _refreshed = load_config()
            config.clear()
            config.update(_refreshed)
        except Exception as exc:
            logger.debug("blank-slate tools_command error: %s", exc)
            print_warning(f"Tool selector encountered an error: {exc}")
    else:
        print_info("Keeping the minimal toolset. Add tools later with `son-of-anton tools`.")

    # ── Built-in plugins (off unless chosen) ──
    print()
    print_header("Plugins")
    if prompt_yes_no("Review and enable built-in plugins now?", default=False):
        print_info("Manage plugins with `son-of-anton plugins list` / `son-of-anton plugins install`.")
    else:
        print_info("No plugins enabled. Add later with `son-of-anton plugins`.")

    # ── MCP servers (off unless chosen) ──
    print()
    print_header("MCP Servers")
    if prompt_yes_no("Add an MCP server now?", default=False):
        print_info("Add servers with `son-of-anton mcp add <name> --url ... | --command ...`.")
    else:
        print_info("No MCP servers configured. Add later with `son-of-anton mcp add`.")

    # ── Optional messaging gateway ──
    print()
    if prompt_yes_no("Connect a messaging platform (Discord, Slack, …)?", default=False):
        setup_gateway(config)

    save_config(config)

    print()
    print_success("Blank Slate setup complete — minimal agent ready.")
    print_info("  Enable more tools:   son-of-anton tools")
    print_info("  Seed skills:         son-of-anton skills opt-in --sync")
    print_info("  Add MCP servers:     son-of-anton mcp add")
    print_info("  Tune agent settings: son-of-anton setup agent")
    print()

    _print_setup_summary(config, son_of_anton_home)


def _run_quick_setup(config: dict, son_of_anton_home):
    """Quick setup — only configure items that are missing."""
    from son_of_anton_cli.config import (
        get_missing_env_vars,
        get_missing_config_fields,
        check_config_version,
    )

    print()
    print_header("Quick Setup — Missing Items Only")

    # Check what's missing
    missing_required = [
        v for v in get_missing_env_vars(required_only=False) if v.get("is_required")
    ]
    missing_optional = [
        v for v in get_missing_env_vars(required_only=False) if not v.get("is_required")
    ]
    missing_config = get_missing_config_fields()
    current_ver, latest_ver = check_config_version()

    has_anything_missing = (
        missing_required
        or missing_optional
        or missing_config
        or current_ver < latest_ver
    )

    if not has_anything_missing:
        print_success("Everything is configured! Nothing to do.")
        print()
        print_info("Run 'son-of-anton setup' and choose 'Full Setup' to reconfigure,")
        print_info("or pick a specific section from the menu.")
        return

    # Handle missing required env vars
    if missing_required:
        print()
        print_info(f"{len(missing_required)} required setting(s) missing:")
        for var in missing_required:
            print(f"     • {var['name']}")
        print()

        for var in missing_required:
            print()
            print(color(f"  {var['name']}", Colors.CYAN))
            print_info(f"  {var.get('description', '')}")
            if var.get("url"):
                print_info(f"  Get key at: {var['url']}")

            if var.get("password"):
                value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
            else:
                value = prompt(f"  {var.get('prompt', var['name'])}")

            if value:
                save_env_value(var["name"], value)
                print_success(f"  Saved {var['name']}")
            else:
                print_warning(f"  Skipped {var['name']}")

    # Split missing optional vars by category
    missing_tools = [v for v in missing_optional if v.get("category") == "tool"]
    missing_messaging = [
        v
        for v in missing_optional
        if v.get("category") == "messaging" and not v.get("advanced")
    ]

    # ── Tool API keys (checklist) ──
    if missing_tools:
        print()
        print_header("Tool API Keys")

        checklist_labels = []
        for var in missing_tools:
            tools = var.get("tools", [])
            tools_str = f" → {', '.join(tools[:2])}" if tools else ""
            checklist_labels.append(f"{var.get('description', var['name'])}{tools_str}")

        selected_indices = prompt_checklist(
            "Which tools would you like to configure?",
            checklist_labels,
        )

        for idx in selected_indices:
            var = missing_tools[idx]
            _prompt_api_key(var)

    # ── Messaging platforms (checklist then prompt for selected) ──
    if missing_messaging:
        print()
        print_header("Messaging Platforms")
        print_info("Connect Son of Anton to messaging apps to chat from anywhere.")
        print_info("You can configure these later with 'son-of-anton setup gateway'.")

        # Group by platform (preserving order)
        platform_order = []
        platforms = {}
        for var in missing_messaging:
            name = var["name"]
            if "DISCORD" in name:
                plat = "Discord"
            elif "SLACK" in name:
                plat = "Slack"
            elif "SIGNAL" in name:
                plat = "Signal"
            else:
                continue
            if plat not in platforms:
                platform_order.append(plat)
            platforms.setdefault(plat, []).append(var)

        platform_labels = [
            {
                "Discord": "💬 Discord",
                "Slack": "💼 Slack",
                "Signal": "📡 Signal",
            }.get(p, p)
            for p in platform_order
        ]

        selected_indices = prompt_checklist(
            "Which platforms would you like to set up?",
            platform_labels,
        )

        for idx in selected_indices:
            plat = platform_order[idx]
            vars_list = platforms[plat]
            emoji = {"Discord": "💬", "Slack": "💼", "Signal": "📡"}.get(plat, "")
            print()
            print(color(f"  ─── {emoji} {plat} ───", Colors.CYAN))
            print()
            for var in vars_list:
                print_info(f"  {var.get('description', '')}")
                if var.get("url"):
                    print_info(f"  {var['url']}")
                if var.get("password"):
                    value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
                else:
                    value = prompt(f"  {var.get('prompt', var['name'])}")
                if value:
                    save_env_value(var["name"], value)
                    print_success("  ✓ Saved")
                else:
                    print_warning("  Skipped")
                print()

    # Handle missing config fields
    if missing_config:
        print()
        print_info(
            f"Adding {len(missing_config)} new config option(s) with defaults..."
        )
        for field in missing_config:
            print_success(f"  Added {field['key']} = {field['default']}")

        # Update config version
        config["_config_version"] = latest_ver
        save_config(config)

    # Jump to summary
    _print_setup_summary(config, son_of_anton_home)
