"""Endpoint probes must auto-resolve context windows for routing proxies.

A LiteLLM-style proxy serves an OpenAI-compatible ``/models`` list whose
entries carry no context metadata — the window lives on the inference server
the request is forwarded to (vLLM reports ``max_model_len``, llama.cpp
reports ``n_ctx``). Before this, resolving a window for such an endpoint
required a user-specified ``context_length``.

The probe now follows the proxy's ``/model/info``: the value the proxy was
configured with (``max_input_tokens``) wins; otherwise the upstream
``api_base`` is fetched and the window read from the inference server's own
``/models``. ``follow_upstream`` defaults to False so callers that only want
pricing don't pay for the extra requests; the context-length resolver
enables it.
"""

from __future__ import annotations

import pytest

import agent.model_metadata as mm

PROXY = "http://proxy.example.invalid/v1"
VLLM = "http://vllm.example.invalid/v1"
LLAMACPP = "http://llamacpp.example.invalid/v1"


class _FakeResponse:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


class _FakeRequests:
    """Dispatches GETs by URL substring to canned responses."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for needle, response in self.routes:
            if needle in url:
                return response
        return _FakeResponse({}, ok=False, status_code=404)


def _proxy_model_info():
    """Shape of LiteLLM's GET /v1/model_info (max_input_tokens unset)."""
    return {
        "data": [
            {
                "model_name": "qwen3.8-27b",
                "litellm_params": {"api_base": VLLM, "model": "openai/Qwen3.8-27B"},
                "model_info": {"max_input_tokens": None, "max_output_tokens": None},
            },
            {
                "model_name": "qwen3.8-flash-next",
                "litellm_params": {"api_base": LLAMACPP, "model": "openai/qwen3.8-flash-next"},
                "model_info": {"max_input_tokens": None, "max_output_tokens": None},
            },
        ]
    }


def _vllm_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "Qwen3.8-27B",
                "object": "model",
                "owned_by": "vllm",
                "max_model_len": 524288,
            }
        ],
    }


def _llamacpp_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "qwen3.8-flash-next",
                "aliases": ["qwen3.8-flash-next"],
                "owned_by": "llamacpp",
                "meta": {"n_ctx": 262144, "n_ctx_train": 262144},
            }
        ],
    }


def _proxy_models():
    """OpenAI-compatible /models as a proxy serves it — no window fields."""
    return {
        "object": "list",
        "data": [
            {"id": "qwen3.8-27b", "object": "model", "owned_by": "openai"},
            {"id": "qwen3.8-flash-next", "object": "model", "owned_by": "openai"},
        ],
    }


@pytest.fixture(autouse=True)
def _fresh_caches():
    mm._endpoint_model_metadata_cache.clear()
    mm._endpoint_model_metadata_cache_time.clear()
    mm._upstream_model_metadata_cache.clear()
    mm._upstream_model_metadata_cache_time.clear()
    yield


def _patch_requests(monkeypatch, routes):
    fake = _FakeRequests(routes)
    monkeypatch.setattr(mm, "requests", fake)
    return fake


def test_default_probe_is_unchanged_and_makes_no_extra_requests(monkeypatch):
    """follow_upstream=False (the pricing path) must not hit /model_info."""
    fake = _patch_requests(
        monkeypatch,
        [("/models", _FakeResponse(_proxy_models())), ("/model/info", _FakeResponse(_proxy_model_info()))],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k")
    assert "qwen3.8-27b" in cache
    assert "context_length" not in cache["qwen3.8-27b"]
    assert not any("/model/info" in url for url in fake.calls)


def test_follow_upstream_resolves_window_from_vllm_upstream(monkeypatch):
    """Routed name (lowercased by the proxy) matches the server's id case-insensitively."""
    _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse(_vllm_models())),
            ("/model/info", _FakeResponse(_proxy_model_info())), ("v1/model/info", _FakeResponse(_proxy_model_info())),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert cache["qwen3.8-27b"]["context_length"] == 524288


def test_follow_upstream_resolves_window_from_llamacpp_upstream(monkeypatch):
    _patch_requests(
        monkeypatch,
        [
            (LLAMACPP + "/models", _FakeResponse(_llamacpp_models())),
            ("/model/info", _FakeResponse(_proxy_model_info())), ("v1/model/info", _FakeResponse(_proxy_model_info())),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert cache["qwen3.8-flash-next"]["context_length"] == 262144


def test_proxy_configured_max_input_tokens_wins_over_upstream(monkeypatch):
    """The operator's declared cap on the proxy must not be shadowed by the upstream."""
    info = _proxy_model_info()
    info["data"][0]["model_info"]["max_input_tokens"] = 131072
    fake = _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse(_vllm_models())),
            ("/model/info", _FakeResponse(info)), ("v1/model/info", _FakeResponse(info)),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert cache["qwen3.8-27b"]["context_length"] == 131072
    # The winning entry must not have triggered an upstream fetch.
    assert not any(VLLM in url for url in fake.calls)


def test_upstream_without_window_field_leaves_entry_unset(monkeypatch):
    _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse({"object": "list", "data": [{"id": "Qwen3.8-27B"}]})),
            ("/model/info", _FakeResponse(_proxy_model_info())), ("v1/model/info", _FakeResponse(_proxy_model_info())),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert "context_length" not in cache["qwen3.8-27b"]


def test_dead_upstream_does_not_poison_the_probe(monkeypatch):
    """A 500 upstream must leave the rest of the fleet resolvable."""
    _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse({}, ok=False, status_code=500)),
            (LLAMACPP + "/models", _FakeResponse(_llamacpp_models())),
            ("/model/info", _FakeResponse(_proxy_model_info())), ("v1/model/info", _FakeResponse(_proxy_model_info())),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert "context_length" not in cache["qwen3.8-27b"]
    assert cache["qwen3.8-flash-next"]["context_length"] == 262144


def test_upstream_fetched_once_per_host_across_models(monkeypatch):
    """Two routed names on one server = one upstream /models fetch."""
    info = _proxy_model_info()
    # Point the second routed name at the same vLLM upstream.
    info["data"][1]["litellm_params"]["api_base"] = VLLM
    vllm_models = _vllm_models()
    vllm_models["data"][0]["id"] = "qwen3.8-flash-next"
    vllm_models["data"][0]["max_model_len"] = 100000
    fake = _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse(vllm_models)),
            ("/model/info", _FakeResponse(info)), ("v1/model/info", _FakeResponse(info)),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert cache["qwen3.8-flash-next"]["context_length"] == 100000
    upstream_fetches = [u for u in fake.calls if VLLM in u]
    assert len(upstream_fetches) == 1


def test_resolver_uses_upstream_enrichment(monkeypatch):
    """The context-length resolver must resolve a window with no user config."""
    _patch_requests(
        monkeypatch,
        [
            (VLLM + "/models", _FakeResponse(_vllm_models())),
            ("/model/info", _FakeResponse(_proxy_model_info())), ("v1/model/info", _FakeResponse(_proxy_model_info())),
            ("proxy.example.invalid/v1/models", _FakeResponse(_proxy_models())),
        ],
    )
    assert mm._resolve_endpoint_context_length("qwen3.8-27b", PROXY, "k") == 524288


def test_model_info_404_falls_back_to_plain_models(monkeypatch):
    """Non-LiteLLM endpoints don't have /model_info — the probe must survive."""
    _patch_requests(
        monkeypatch,
        [
            ("/model/info", _FakeResponse({}, ok=False, status_code=404)),
            (PROXY + "/models", _FakeResponse({"object": "list", "data": [
                {"id": "some-model", "owned_by": "vllm", "max_model_len": 32768}
            ]})),
        ],
    )
    cache = mm.fetch_endpoint_model_metadata(PROXY, api_key="k", follow_upstream=True)
    assert cache["some-model"]["context_length"] == 32768
