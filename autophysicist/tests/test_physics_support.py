"""The physics mode's own copies of what it used to import from the agent."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from autophysicist import support


def test_the_home_follows_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SON_OF_ANTON_HOME", str(tmp_path))
    assert support.home() == tmp_path
    monkeypatch.delenv("SON_OF_ANTON_HOME")
    assert support.home() == Path.home() / ".son-of-anton"


def test_config_is_the_homes_toml_and_never_raises(son_of_anton_home) -> None:
    assert support.load_config() == {}
    (son_of_anton_home / "config.toml").write_text("[physics]\nmax_iterations = 3\n", encoding="utf-8")
    assert support.load_config() == {"physics": {"max_iterations": 3}}
    (son_of_anton_home / "config.toml").write_text("[physics\n", encoding="utf-8")
    assert support.load_config() == {}


def test_dotenv_overrides_the_environment_and_strips_credential_noise(son_of_anton_home, monkeypatch) -> None:
    (son_of_anton_home / ".env").write_text(
        "# comment\n"
        "A_PLAIN=one\n"
        "export B_QUOTED=\"two words\"  \n"
        "C_SINGLE='three'\n"
        "D_INLINE=four # trailing\n"
        "SECRET_API_KEY=kʋey\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("A_PLAIN", "stale")
    for name in ("B_QUOTED", "C_SINGLE", "D_INLINE", "SECRET_API_KEY", "EMPTY"):
        monkeypatch.delenv(name, raising=False)
    assert support.load_dotenv() == [son_of_anton_home / ".env"]
    assert os.environ["A_PLAIN"] == "one"
    assert os.environ["B_QUOTED"] == "two words"
    assert os.environ["C_SINGLE"] == "three"
    assert os.environ["D_INLINE"] == "four"
    assert os.environ["SECRET_API_KEY"] == "key"
    assert os.environ["EMPTY"] == ""
    for name in ("A_PLAIN", "B_QUOTED", "C_SINGLE", "D_INLINE", "SECRET_API_KEY", "EMPTY"):
        monkeypatch.delenv(name)


def test_dotenv_without_a_file_loads_nothing() -> None:
    assert support.load_dotenv() == []


def test_a_dumped_document_reloads_as_it_was() -> None:
    data = {
        "name": "x",
        "n": 3,
        "f": 1.5,
        "flag": False,
        "list": ["a", "b"],
        "dropped": None,
        "tbl": {"k": "v", "nested": {"z": 1}},
        "checks": [{"id": "a", "expected": 1.0}, {"id": "b", "expected": 2.0}],
        "odd key": "q",
    }
    expected = {k: v for k, v in data.items() if v is not None}
    assert tomllib.loads(support.dump_toml(data)) == expected


def test_dump_refuses_what_toml_cannot_hold() -> None:
    import pytest

    with pytest.raises(TypeError):
        support.dump_toml({"x": object()})
    with pytest.raises(TypeError):
        support.dump_toml({"x": [1, {"a": 2}]})


def test_clamp_keeps_a_supported_level_and_weakens_an_unsupported_one() -> None:
    wire = ("none", "low", "medium", "xhigh")
    assert support.clamp_effort("medium", wire) == "medium"
    assert support.clamp_effort("high", wire) == "medium"  # nearest weaker
    assert support.clamp_effort("max", wire) == "xhigh"
    assert support.clamp_effort("minimal", wire) == "low"  # nothing weaker but none: weakest
    assert support.clamp_effort("high", None) == "high"
    assert support.clamp_effort("weird", wire) == "weird"
    assert support.clamp_effort("", wire) == ""


def _config(url: str) -> dict:
    return {
        "custom_providers": {
            "local": {
                "base_url": url,
                "models": {"m": {"context_length": 4096, "reasoning_efforts": ["Low", "xhigh"]}},
            }
        }
    }


def test_a_route_is_matched_by_its_url_not_its_spelling() -> None:
    config = _config("HTTP://Host:80/v1/")
    assert support.reasoning_decl("m", "http://host/v1", config) == {"efforts": ("low", "xhigh")}
    assert support.reasoning_decl("m", "http://other/v1", config) == {}
    assert support.reasoning_decl("unknown", "http://host/v1", config) == {}
    assert support.context_length("m", "http://host/v1", config=config) == 4096


def test_context_length_is_zero_when_nothing_says(monkeypatch) -> None:
    import httpx

    def refuse(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(support.httpx, "get", refuse)
    assert support.context_length("m", "http://127.0.0.1:1/v1", config={}) == 0
    assert support.context_length("", "http://127.0.0.1:1/v1", config={}) == 0


def test_context_length_reads_the_endpoints_model_list(monkeypatch) -> None:
    class Reply:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return {"data": self._data}

    models = [{"id": "a", "context_length": 1000}, {"id": "b", "meta": {"n_ctx": 2000}}]
    monkeypatch.setattr(support.httpx, "get", lambda *a, **k: Reply(models))
    assert support.context_length("a", "http://h/v1", config={}) == 1000
    assert support.context_length("b", "http://h/v1", config={}) == 2000
    assert support.context_length("c", "http://h/v1", config={}) == 0
    monkeypatch.setattr(support.httpx, "get", lambda *a, **k: Reply(models[:1]))
    assert support.context_length("anything", "http://h/v1", config={}) == 1000
