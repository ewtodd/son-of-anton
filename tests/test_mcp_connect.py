"""Contract: a configured MCP server connects and its tools are callable.

The deployments reach maitred, the physics roles and the project-agent bridges
only through MCP, so discovery silently returning nothing (as when the SDK
check reported "not available" with the SDK installed) loses every MCP tool.
"""

from __future__ import annotations

import json
import sys

import tools.mcp_tool as mcp_tool
from model_tools import handle_function_call

SERVER_SOURCE = '''
import asyncio
from mcp.server import MCPServer

server = MCPServer("connect-test")


@server.tool()
def shout(text: str) -> str:
    """Upper-case the text."""
    return "MCP:" + text.upper()


asyncio.run(server.run_stdio_async())
'''


def test_a_stdio_server_connects_and_its_tool_answers(son_of_anton_home, tmp_path) -> None:
    script = tmp_path / "server.py"
    script.write_text(SERVER_SOURCE, encoding="utf-8")
    (son_of_anton_home / "config.toml").write_text(
        f'[mcp_servers.echo]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(str(script))}]\n',
        encoding="utf-8",
    )
    try:
        names = mcp_tool.discover_mcp_tools()
        assert "mcp__echo__shout" in names
        assert [s["status"] for s in mcp_tool.get_mcp_status()] == ["connected"]
        result = handle_function_call("mcp__echo__shout", {"text": "hi"}, task_id="t")
        assert "MCP:HI" in result
    finally:
        mcp_tool.shutdown_mcp_servers()
