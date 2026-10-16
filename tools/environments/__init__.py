"""Son of Anton execution environment.

``LocalEnvironment`` (local.py) runs shell commands on the host through the
``BaseEnvironment`` execution flow (base.py); terminal_tool.py creates it.
"""

from tools.environments.base import BaseEnvironment

__all__ = ["BaseEnvironment"]
