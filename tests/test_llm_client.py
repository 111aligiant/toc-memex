import httpx
import pytest

from app.config import Settings
from app.llm.client import LLMError, extract_json, llm_available, llm_complete


def _settings(**overrides) -> Settings:
    base = dict(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
    )
    base.update(overrides)
    return Settings(**base)


def _ok_response(content: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": content}}]},
    )


def test_extract_json_plain_and_fenced_and_prose():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]
    assert extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json('前置说明\n[{"a": 1}] 后置') == [{"a": 1}]


def test_extract_json_garbage_raises():
    with pytest.raises(ValueError):
        extract_json('完全不是 JSON')


def test_llm_available_false_without_key():
    s = Settings(tocmemex_llm_api_key="", tocmemex_llm_model="m")
    assert llm_available(s) is False
    s2 = Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
    )
    assert llm_available(s2) is True


def test_llm_complete_posts_and_extracts_content(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json

        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "hello"}}]}

        return R()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    assert llm_complete("p", _settings()) == "hello"
    assert captured["url"] == "http://x/v1/chat/completions"
    assert captured["json"]["messages"][0]["role"] == "user"


def test_llm_complete_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_post(self, *args, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError):
        llm_complete("p", _settings(), retries=1)
    assert calls["n"] == 2


def test_llm_complete_reasoning_only_response(monkeypatch):
    """Reasoning models can spend the whole max_tokens budget on reasoning,
    leaving content null. The endpoint test-ping must treat that as reachable."""

    def fake_post(self, url, json=None, headers=None, timeout=None):
        class R:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": None, "reasoning_content": "Ef"},
                        }
                    ]
                }

        return R()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    s = _settings()
    with pytest.raises(LLMError):
        llm_complete("p", s, max_tokens=1)  # default: null content is a failure
    assert llm_complete("p", s, max_tokens=1, require_content=False) == ""


def test_llm_complete_sends_reasoning_effort(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _ok_response("hi")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    s = _settings(tocmemex_llm_reasoning_effort="low")
    llm_complete("p", s)
    assert captured["json"]["reasoning_effort"] == "low"


def test_llm_complete_omits_empty_reasoning_effort(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _ok_response("hi")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    llm_complete("p", _settings())
    assert "reasoning_effort" not in captured["json"]


def test_chat_message_to_wire_includes_image_parts():
    from app.llm.client import ChatMessage

    msg = ChatMessage(
        role="user",
        content="看图",
        images=[{"mime": "image/png", "data_url": "data:image/png;base64,AAA"}],
    )
    wire = msg.to_wire()
    assert wire["content"][0] == {"type": "text", "text": "看图"}
    assert wire["content"][1]["type"] == "image_url"
    assert wire["content"][1]["image_url"]["url"].startswith("data:image/png")


def test_chat_message_without_images_keeps_plain_content():
    from app.llm.client import ChatMessage

    assert ChatMessage(role="user", content="看图").to_wire() == {
        "role": "user",
        "content": "看图",
    }


def test_llm_chat_drops_images_on_endpoint_rejection(monkeypatch):
    import json

    from app.llm.client import ChatMessage, llm_chat

    posted: list[dict] = []

    def fake_post(self, url, json=None, headers=None, timeout=None):
        posted.append(json)

        class R:
            status_code = 400 if len(posted) == 1 else 200
            text = "image input is not supported"

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        return R()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    message = ChatMessage(
        role="user",
        content="看图",
        images=[{"mime": "image/png", "data_url": "data:image/png;base64,AAA"}],
    )
    reply = llm_chat([message], _settings(), retries=0)
    assert reply.images_dropped is True
    assert reply.content == "ok"
    assert "image_url" not in json.dumps(posted[1])
    assert "base64" not in json.dumps(posted[1])
    assert "图片输入被端点拒绝" in json.dumps(posted[1], ensure_ascii=False)
