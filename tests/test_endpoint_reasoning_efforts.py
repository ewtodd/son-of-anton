"""A custom route's effort vocabulary can come from the endpoint, like its window.

A front such as maitred lists, per model in ``/models``, the
``reasoning_efforts`` its server takes (weakest first) and the explicit
rewrites it applies (``reasoning_effort_aliases``), next to
``context_length``. Contracts:

- the probe (which holds the route's key) caches the listed vocabulary, and
  the custom profile — which holds no key and makes no request — clamps
  against it when the route declares no set of its own;
- every level sent is one the endpoint listed, and a listed alias decides
  the level where it applies;
- a set declared in config.toml still wins;
- a lookup without a key never requests ``/models`` (a cached 401 would hide
  the endpoint's window from the keyed context probe).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.model_metadata as mm
from agent.chat_completion_helpers import _probe_endpoint_reasoning_decl
from agent.reasoning_effort import EFFORT_LADDER, custom_reasoning_extras, supported_efforts_for

GATEWAY = "http://gateway.invalid:4002/v1"

DSV4 = "unholy/dsv4-flash"
DSV4_EFFORTS = ("none", "low", "high", "max")
DSV4_ALIASES = {"medium": "high", "minimal": "low", "xhigh": "max"}
QWEN = "radiance/qwen3.8-27b"
QWEN_EFFORTS = ("none", "low", "medium", "xhigh")
API = "deepseek/deepseek-flash"
API_EFFORTS = ("low", "high", "max")


def _gateway_models():
    """/v1/models as maitred serves it."""
    return {
        "object": "list",
        "data": [
            {
                "id": DSV4,
                "object": "model",
                "owned_by": "unholy",
                "context_length": 524288,
                "reasoning_efforts": list(DSV4_EFFORTS),
                "default_reasoning_effort": "high",
                "reasoning_effort_aliases": dict(DSV4_ALIASES),
                "status": "serving",
            },
            {
                "id": QWEN,
                "object": "model",
                "owned_by": "radiance",
                "context_length": 352256,
                "reasoning_efforts": list(QWEN_EFFORTS),
                "default_reasoning_effort": "xhigh",
                "status": "unavailable",
            },
            {
                "id": API,
                "object": "model",
                "owned_by": "deepseek",
                "reasoning_efforts": list(API_EFFORTS),
            },
            {"id": "embed/bge-m3", "object": "model", "owned_by": "embed"},
        ],
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


class _FakeRequests:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        if url.endswith("/models"):
            return _FakeResponse(self.payload)
        return _FakeResponse({}, status_code=404)


@pytest.fixture(autouse=True)
def _fresh_caches():
    mm._endpoint_model_metadata_cache.clear()
    mm._endpoint_model_metadata_cache_time.clear()
    yield
    mm._endpoint_model_metadata_cache.clear()
    mm._endpoint_model_metadata_cache_time.clear()


@pytest.fixture
def gateway(monkeypatch):
    fake = _FakeRequests(_gateway_models())
    monkeypatch.setattr(mm, "requests", fake)
    return fake


def _probe(model, api_key="sk-route"):
    agent = SimpleNamespace(provider="custom", base_url=GATEWAY, model=model, api_key=api_key)
    _probe_endpoint_reasoning_decl(agent)


def _sent(model, effort):
    _extra, top = custom_reasoning_extras(
        {"enabled": True, "effort": effort}, model=model, base_url=GATEWAY
    )
    return top.get("reasoning_effort")


def test_probe_caches_the_listed_vocabulary_with_the_route_key(gateway) -> None:
    _probe(DSV4)
    assert gateway.calls, "the probe must fetch /models"
    assert all(h.get("Authorization") == "Bearer sk-route" for _url, h in gateway.calls)
    decl = mm.get_endpoint_reasoning_decl(DSV4, GATEWAY)
    assert decl["efforts"] == DSV4_EFFORTS
    assert decl["effort"] in decl["efforts"]
    assert decl["aliases"] == DSV4_ALIASES
    # A model listed without a vocabulary carries none.
    assert mm.get_endpoint_reasoning_decl("embed/bge-m3", GATEWAY) == {}


def test_a_lookup_without_a_key_never_requests(gateway) -> None:
    assert mm.get_endpoint_reasoning_decl(DSV4, GATEWAY) == {}
    assert supported_efforts_for(DSV4, provider="custom", base_url=GATEWAY)
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("model", "listed"), [(DSV4, DSV4_EFFORTS), (QWEN, QWEN_EFFORTS), (API, API_EFFORTS)]
)
def test_every_level_sent_is_one_the_endpoint_listed(gateway, model, listed) -> None:
    _probe(model)
    for effort in EFFORT_LADDER:
        if effort == "none":
            continue  # the disable path, below
        assert _sent(model, effort) in listed, effort


def test_clamping_never_escalates_without_an_alias(gateway) -> None:
    _probe(QWEN)
    for effort in EFFORT_LADDER[1:]:
        sent = _sent(QWEN, effort)
        if effort in QWEN_EFFORTS:
            assert sent == effort
        else:
            weaker = [lvl for lvl in QWEN_EFFORTS[1:] if EFFORT_LADDER.index(lvl) < EFFORT_LADDER.index(effort)]
            assert sent == (weaker[-1] if weaker else QWEN_EFFORTS[1]), effort


def test_a_listed_alias_decides_the_level(gateway) -> None:
    _probe(DSV4)
    for asked, wire in DSV4_ALIASES.items():
        assert _sent(DSV4, asked) == wire


def test_disabling_thinking_omits_none_where_the_endpoint_lacks_it(gateway) -> None:
    _probe(API)
    _extra, top = custom_reasoning_extras(
        {"enabled": False}, model=API, base_url=GATEWAY
    )
    assert "reasoning_effort" not in top
    _probe(DSV4)
    _extra, top = custom_reasoning_extras(
        {"enabled": False}, model=DSV4, base_url=GATEWAY
    )
    assert top.get("reasoning_effort") == "none"


def test_the_picker_offers_the_listed_vocabulary(gateway) -> None:
    _probe(QWEN)
    assert supported_efforts_for(QWEN, provider="custom", base_url=GATEWAY) == QWEN_EFFORTS


def test_a_declared_set_wins_over_the_listed_one(gateway, son_of_anton_home) -> None:
    declared = ("low", "medium")
    (son_of_anton_home / "config.toml").write_text(
        f"""
[custom_providers.custom]
base_url = "{GATEWAY}"

[custom_providers.custom.models."{DSV4}"]
reasoning_efforts = {list(declared)!r}
""".replace("'", '"'),
        encoding="utf-8",
    )
    _probe(DSV4)
    for effort in EFFORT_LADDER[1:]:
        assert _sent(DSV4, effort) in declared, effort
    assert supported_efforts_for(DSV4, provider="custom", base_url=GATEWAY) == declared

