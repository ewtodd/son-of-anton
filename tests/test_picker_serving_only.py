"""The /model picker lists what a gateway is serving, not everything it fronts.

maitred lists every model of every mode, loaded or not, each with a live
``status``. Contracts, through the real picker path against a real HTTP
endpoint:

- the picker lists only the models not marked unavailable;
- a mode switch shows on the next open, though the catalog cache is an hour
  fresh;
- the catalog itself keeps every model, so naming an unloaded one stays valid;
- an endpoint that reports no statuses is unfiltered and costs the picker no
  extra round-trip.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from son_of_anton_cli.model_switch import list_authenticated_providers
from son_of_anton_cli.models import cached_fetch_api_models

DS4 = "unholy/dsv4-flash"
QWEN = ["radiance/qwen3.8-27b", "gufo/qwen3.8-flash-next"]


class _Catalog:
    """A ``/v1/models`` endpoint whose listing a test can change, counting requests."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.requests = 0
        catalog = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server's name)
                catalog.requests += 1
                body = json.dumps({"object": "list", "data": catalog.entries}).encode()
                self.send_response(200 if self.path == "/v1/models" else 404)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _maitred(serving: set[str]) -> list[dict]:
    ids = ["auto", DS4, *QWEN]
    return [
        {"id": i, "object": "model", "status": "serving" if i in serving else "unavailable"}
        for i in ids
    ]


@pytest.fixture
def catalog():
    made: list[_Catalog] = []

    def make(entries: list[dict]) -> _Catalog:
        c = _Catalog(entries)
        made.append(c)
        return c

    yield make
    for c in made:
        c.close()


def _picker(base_url: str) -> list[str]:
    rows = list_authenticated_providers(
        current_provider="custom",
        current_base_url=base_url,
        custom_providers=[
            {
                "name": "custom",
                "base_url": base_url,
                "api_key": "test-key",
                "discover_models": True,
                # Per-model metadata, not an allowlist (discover_models is on).
                "models": {DS4: {"reasoning_effort": "high"}},
            }
        ],
        probe_custom_providers=True,
        probe_current_custom_provider=True,
        for_picker=True,
    )
    row = next(r for r in rows if r.get("api_url") == base_url)
    return list(row["models"])


def test_the_picker_lists_only_what_is_served_and_follows_a_switch(catalog, monkeypatch) -> None:
    gateway = catalog(_maitred({"auto", DS4}))
    assert _picker(gateway.base_url) == ["auto", DS4]

    # son-of-anton's GPUs switch to the Qwen pair. The catalog cache is still
    # fresh; the statuses are asked again once they are older than the window.
    gateway.entries = _maitred({"auto", *QWEN})
    monkeypatch.setattr("son_of_anton_cli.models._LIVE_STATUS_TTL", 0)
    assert _picker(gateway.base_url) == ["auto", *QWEN]


def test_the_catalog_keeps_every_model(catalog) -> None:
    gateway = catalog(_maitred({"auto", DS4}))
    _picker(gateway.base_url)
    # Naming a model that is not loaded stays valid: the gateway answers why.
    every = cached_fetch_api_models("test-key", gateway.base_url, cache_only=True)
    assert set(every) == {"auto", DS4, *QWEN}


def test_an_endpoint_without_statuses_is_unfiltered_and_costs_nothing_more(catalog) -> None:
    plain = catalog([{"id": "llama-3"}, {"id": "qwen-coder"}])
    assert _picker(plain.base_url) == ["llama-3", "qwen-coder"]
    asked = plain.requests
    # A second open inside the catalog cache's freshness makes no request.
    assert _picker(plain.base_url) == ["llama-3", "qwen-coder"]
    assert plain.requests == asked
