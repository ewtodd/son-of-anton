"""Son of Anton Textual front-end.

``tui`` is the app; ``backend`` is its client of the soa-d daemon
(``DaemonBackend``), which owns the session and runs every turn.
"""

__all__ = ["SonOfAntonTUIApp", "DaemonBackend", "run_app"]

from son_of_anton_tui.backend import DaemonBackend  # noqa: E402
from son_of_anton_tui.tui import SonOfAntonTUIApp, run_app  # noqa: E402
