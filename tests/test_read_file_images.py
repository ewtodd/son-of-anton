"""read_file hands images to vision-capable models instead of refusing them.

The old binary-extension guard sent every image to vision_analyze even when
the active model could see pixels, costing an extra model call and a lossy
text summary. read_file now returns the ``_multimodal`` envelope the executor
already knows how to downgrade for text-only models.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image


def _encoded_image(width: int = 8, height: int = 8, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def read_image(tmp_path):
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import cleanup_vm

    task_id = "read-file-image"

    def read(data: bytes, filename: str = "picture.png") -> object:
        path = tmp_path / filename
        path.write_bytes(data)
        return read_file_tool(str(path), task_id=task_id)

    try:
        yield read
    finally:
        cleanup_vm(task_id)


def _image_part_url(result: dict) -> str:
    parts = result["content"]
    assert parts[0]["type"] == "text"
    image_part = parts[1]
    assert image_part["type"] == "image_url"
    return image_part["image_url"]["url"]


def test_image_returns_multimodal_envelope(read_image) -> None:
    result = read_image(_encoded_image())

    assert isinstance(result, dict), result
    assert result["_multimodal"] is True
    text = result["content"][0]["text"]
    assert "picture.png" in text and "image/png" in text

    url = _image_part_url(result)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]).startswith(b"\x89PNG")


def test_text_only_fallback_names_vision_analyze(read_image) -> None:
    result = read_image(_encoded_image())

    summary = result["text_summary"]
    assert "picture.png" in summary
    assert "vision_analyze" in summary


def test_large_dimensions_are_downscaled(read_image) -> None:
    result = read_image(_encoded_image(width=2500, height=2500))

    url = _image_part_url(result)
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as img:
        assert max(img.size) <= 2000


def test_bmp_is_transcoded_to_png(read_image) -> None:
    result = read_image(_encoded_image(fmt="BMP"), filename="picture.bmp")

    assert _image_part_url(result).startswith("data:image/png;base64,")


def test_text_file_still_reads_as_text(tmp_path) -> None:
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import cleanup_vm

    task_id = "read-file-text"
    path = tmp_path / "notes.txt"
    path.write_text("first line", encoding="utf-8")
    try:
        result = read_file_tool(str(path), task_id=task_id)
    finally:
        cleanup_vm(task_id)

    assert isinstance(result, str)
    assert "1|first line" in result


def test_registry_dispatch_preserves_the_envelope(tmp_path) -> None:
    from model_tools import handle_function_call
    from tools.terminal_tool import cleanup_vm

    path = tmp_path / "dispatched.png"
    path.write_bytes(_encoded_image())
    task_id = "read-file-image-dispatch"
    try:
        result = handle_function_call(
            "read_file", {"path": str(path)}, task_id=task_id
        )
    finally:
        cleanup_vm(task_id)

    assert isinstance(result, dict), result
    assert result["_multimodal"] is True


def test_non_image_binary_still_refused(tmp_path) -> None:
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import cleanup_vm

    task_id = "read-file-binary"
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04 not really a zip")
    try:
        result = read_file_tool(str(path), task_id=task_id)
    finally:
        cleanup_vm(task_id)

    assert isinstance(result, str)
    assert "binary" in result.lower()


def _bare_agent() -> object:
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.provider = "custom"
    agent.model = "unlisted-model"
    agent._no_list_tool_content_models = set()
    return agent


def test_non_vision_model_gets_the_text_summary(monkeypatch, read_image) -> None:
    import son_of_anton_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"agent": {"image_input_mode": "auto"}},
    )
    result = read_image(_encoded_image())
    agent = _bare_agent()

    content = agent._tool_result_content_for_active_model("read_file", result)

    assert isinstance(content, str)
    assert "vision_analyze" in content


def test_native_mode_keeps_the_image_for_an_undeclared_model(
    monkeypatch, read_image
) -> None:
    import son_of_anton_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"agent": {"image_input_mode": "native"}},
    )
    result = read_image(_encoded_image())
    agent = _bare_agent()

    content = agent._tool_result_content_for_active_model("read_file", result)

    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"


def test_native_mode_keeps_images_on_the_wire(monkeypatch) -> None:
    import son_of_anton_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"agent": {"image_input_mode": "native"}},
    )
    agent = _bare_agent()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]

    prepared = agent._prepare_messages_for_non_vision_model(messages)

    assert prepared is messages


def test_native_mode_still_falls_back_after_a_provider_rejection(
    monkeypatch, read_image
) -> None:
    import son_of_anton_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"agent": {"image_input_mode": "native"}},
    )
    result = read_image(_encoded_image())
    agent = _bare_agent()
    agent._vision_supported = False

    content = agent._tool_result_content_for_active_model("read_file", result)

    assert isinstance(content, str)
    assert "vision_analyze" in content


def test_text_mode_wins_over_declared_vision(monkeypatch, read_image) -> None:
    import son_of_anton_cli.config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "agent": {"image_input_mode": "text"},
            "model": {"supports_vision": True},
        },
    )
    result = read_image(_encoded_image())
    agent = _bare_agent()

    content = agent._tool_result_content_for_active_model("read_file", result)

    assert isinstance(content, str)
    assert "vision_analyze" in content


def test_turn_budget_leaves_multimodal_content_alone() -> None:
    from tools.budget_config import BudgetConfig
    from tools.tool_result_storage import enforce_turn_budget

    image_message = {
        "role": "tool",
        "content": [
            {"type": "text", "text": "image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "tool_call_id": "img",
    }
    text_message = {"role": "tool", "content": "x" * 5000, "tool_call_id": "txt"}
    original_text = text_message["content"]
    messages = [image_message, text_message]

    enforce_turn_budget(
        messages, config=BudgetConfig(turn_budget=100, default_result_size=100)
    )

    assert messages[0] is image_message
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png")
    assert messages[1]["content"] != original_text
