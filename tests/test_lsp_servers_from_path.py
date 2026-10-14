"""Language servers come from PATH (the Nix build), never from a runtime install.

The LSP layer used to npm/go/pip-install a missing server into the home
directory the first time the agent edited such a file. Servers now come only
from PATH — the package's ``extraPackages``, wired to the servers the
user's editor config enables — and a missing one is skipped.
"""

from __future__ import annotations

import importlib
import os
import stat

import pytest


def _fake_bin(directory, name: str) -> str:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_there_is_no_runtime_installer() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent.lsp.install")


def test_a_server_missing_from_path_is_skipped(tmp_path, monkeypatch) -> None:
    from agent.lsp.servers import SERVERS, ServerContext

    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    ctx = ServerContext(workspace_root=str(tmp_path))
    for server in SERVERS:
        if server.server_id == "powershell":
            continue  # resolves a bundle directory, not a PATH binary
        assert server.build_spawn(str(tmp_path), ctx) is None, server.server_id


def test_python_prefers_basedpyright_and_falls_back_to_pyright(tmp_path, monkeypatch) -> None:
    from agent.lsp.servers import ServerContext, find_server_for_file

    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    server = find_server_for_file(str(tmp_path / "mod.py"))
    ctx = ServerContext(workspace_root=str(tmp_path))

    pyright = _fake_bin(bindir, "pyright-langserver")
    assert server.build_spawn(str(tmp_path), ctx).command[0] == pyright

    based = _fake_bin(bindir, "basedpyright-langserver")
    assert server.build_spawn(str(tmp_path), ctx).command[0] == based


@pytest.mark.parametrize(
    "filename, server_id, binary",
    [
        ("paper.tex", "texlab", "texlab"),
        ("refs.bib", "texlab", "texlab"),
        ("config.json", "json-language-server", "vscode-json-language-server"),
    ],
)
def test_editor_servers_handle_their_files(tmp_path, monkeypatch, filename, server_id, binary) -> None:
    from agent.lsp.servers import ServerContext, find_server_for_file

    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    expected = _fake_bin(bindir, binary)

    server = find_server_for_file(os.path.join(str(tmp_path), filename))
    assert server is not None and server.server_id == server_id
    spec = server.build_spawn(str(tmp_path), ServerContext(workspace_root=str(tmp_path)))
    assert spec is not None and spec.command[0] == expected
