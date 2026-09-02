"""Son of Anton Textual front-end.

``tui`` is the app; ``backend`` wraps the classic ``SonOfAntonCLI`` so the
agent loop, slash commands and modal prompts are reused unchanged.
"""

__all__ = ["SonOfAntonTUIApp", "TextualBackend", "run_app"]

from son_of_anton_tui.tui import SonOfAntonTUIApp, run_app  # noqa: E402


def __getattr__(name):
    # ``backend`` imports ``cli`` (heavy); resolve it lazily.
    if name == "TextualBackend":
        from son_of_anton_tui.backend import TextualBackend

        return TextualBackend
    raise AttributeError(name)
