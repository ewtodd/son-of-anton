"""The chat.completions transport and its normalized response types."""

from agent.transports.types import (
    NormalizedResponse as NormalizedResponse,
    ToolCall as ToolCall,
    Usage as Usage,
)  # noqa: F401


def get_transport(api_mode: str = "chat_completions"):
    """Return the chat.completions transport (the only wire this fork speaks)."""
    from agent.transports.chat_completions import ChatCompletionsTransport

    return ChatCompletionsTransport()
