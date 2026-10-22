"""``son-of-anton -z``: exit 0 and stdout only for a run that completed.

The agent loop reports a failed or cut-short turn by putting its error text in
``final_response`` (chat surfaces display it). One-shot printed that as the
answer and exited 0 — a model-not-loaded 503 from maitred read as a
successful run with "HTTP 503: … is not loaded" for an answer. Driven through
the real ``run_oneshot``, ``AIAgent`` and OpenAI client against an in-process
endpoint that answers like maitred.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import son_of_anton_cli.oneshot as oneshot

NOT_LOADED = (
    "DeepSeek-V4-Flash (unholy/dsv4-flash) is not loaded: son-of-anton is serving "
    "the Qwen models right now. Use Qwen3.8-27B (radiance/qwen3.8-27b) instead."
)


class FakeGateway:
    """Chat completions that either answer or refuse as an unloaded model."""

    def __init__(self, answer: str | None) -> None:
        gateway = self
        self.answer = answer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self._json(200, {"object": "list", "data": [{"id": "fake", "object": "model"}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if gateway.answer is None:
                    self._json(503, {"error": {
                        "message": NOT_LOADED, "type": "model_unavailable", "code": "model_not_loaded",
                    }})
                    return
                if not body.get("stream"):
                    self._json(200, {
                        "id": "aux", "object": "chat.completion", "created": 1, "model": "fake",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "Title"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake"}
                for delta, finish in (({"role": "assistant", "content": gateway.answer}, None), ({}, "stop")):
                    chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
                self.wfile.write(f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def _json(self, status: int, payload) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def oneshot_env(son_of_anton_home, tmp_path, monkeypatch):
    for var in ("TERMINAL_CWD", "SON_OF_ANTON_YOLO_MODE", "SON_OF_ANTON_SESSION_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    servers = []

    def configure(answer):
        from son_of_anton_cli.config import save_config

        gateway = FakeGateway(answer)
        servers.append(gateway)
        save_config({
            "model": {"default": "fake", "provider": "custom"},
            "custom_providers": {"custom": {"base_url": gateway.base_url, "api_key": "sk-fake"}},
        })

    yield configure
    for gateway in servers:
        gateway.close()
    logging.disable(logging.NOTSET)


def test_a_model_that_is_not_loaded_fails_the_run(oneshot_env, tmp_path, capsys) -> None:
    oneshot_env(None)
    usage = tmp_path / "usage.json"
    rc = oneshot.run_oneshot("hi", usage_file=str(usage))
    out, err = capsys.readouterr()

    assert rc == 2
    assert out == "", "an error message must never read as the answer"
    assert "is not loaded" in err
    assert json.loads(usage.read_text(encoding="utf-8"))["failed"] is True


def test_a_completed_answer_is_the_only_thing_on_stdout(oneshot_env, tmp_path, capsys) -> None:
    oneshot_env("the answer")
    usage = tmp_path / "usage.json"
    rc = oneshot.run_oneshot("hi", usage_file=str(usage))
    out, _err = capsys.readouterr()

    assert rc == 0
    assert out == "the answer\n"
    assert json.loads(usage.read_text(encoding="utf-8"))["failed"] is False


@pytest.mark.parametrize(
    "result",
    [
        # A truncated reply: real text, but not the whole answer.
        {"final_response": "half an ans", "completed": False, "partial": True,
         "error": "Response remained truncated after 4 continuation attempts"},
        # The iteration cap: the model's wrap-up summary, not the answer.
        {"final_response": "Here is what I got through…", "completed": False,
         "turn_exit_reason": "max_iterations_reached(500/500)"},
    ],
    ids=["partial", "iteration-cap"],
)
def test_a_run_cut_short_exits_nonzero_with_its_text_on_stderr(monkeypatch, capsys, result) -> None:
    monkeypatch.delenv("SON_OF_ANTON_YOLO_MODE", raising=False)
    monkeypatch.delenv("SON_OF_ANTON_SESSION_SOURCE", raising=False)
    monkeypatch.setattr(oneshot, "_run_agent", lambda prompt, **kw: (result["final_response"], result))
    try:
        rc = oneshot.run_oneshot("hi")
    finally:
        logging.disable(logging.NOTSET)
    out, err = capsys.readouterr()

    assert rc == 2
    assert out == ""
    assert result["final_response"] in err
