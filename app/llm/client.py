"""Minimal OpenAI-compatible LLM client for ingest-time TOC parsing.

The ingest layer's only LLM consumers are the TOC detection/extraction/
verify/fix helpers in ``app/ingest/llm_toc.py``. Everything goes through
``llm_complete`` / ``llm_available`` / ``extract_json``; no other module
talks to an LLM endpoint directly.

``extract_json`` is ported from PageIndex (VectifyAI/PageIndex, MIT),
``pageindex/utils.py::extract_json`` — see vendor/pageindex/NOTICE.md.

No API key configured means every LLM feature is off: callers check
``llm_available`` first and fall back to the rule-based path.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings

_RETRY_DELAY_SECONDS = 2.0


class LLMError(RuntimeError):
    """An LLM call failed after retries (network, status, or bad shape)."""


def llm_available(settings: Settings | None = None) -> bool:
    """True when key, base URL and model are all configured."""
    settings = settings or get_settings()
    return bool(
        settings.tocmemex_llm_api_key
        and settings.tocmemex_llm_base_url
        and settings.tocmemex_llm_model
    )


def llm_complete(
    prompt: str,
    settings: Settings | None = None,
    *,
    max_tokens: int | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    require_content: bool = True,
    call_site: str = "llm_complete",
    run_id: str | None = None,
    doc_id: str | None = None,
    tools: list[dict] | None = None,
) -> str:
    """POST one chat completion; return the message content.

    Raises LLMError on connection failure (after ``retries`` retries),
    non-200 status, or a response without choices. Reasoning models can
    burn a tiny ``max_tokens`` budget entirely on reasoning, leaving
    ``content`` null; callers doing reachability pings pass
    ``require_content=False`` to accept that as success (empty string).

    ``max_tokens`` / ``retries`` / ``timeout`` default to the resolved
    Settings values (per-agent knobs land there via
    ``app.llm.purpose.resolve_runtime``); an explicit argument still wins.

    Every call is timed and its token usage logged to ``data/llm/calls.jsonl``
    (ADR-0002 §10). Logging never raises; a failed call is logged too, so the
    usage view can show errors alongside cost.
    """
    settings = settings or get_settings()
    max_tokens = settings.tocmemex_llm_max_tokens if max_tokens is None else max_tokens
    retries = settings.tocmemex_llm_retries if retries is None else retries
    timeout = settings.tocmemex_llm_timeout if timeout is None else timeout
    body = {
        "model": settings.tocmemex_llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": settings.tocmemex_llm_temperature,
        "max_tokens": max_tokens,
    }
    if settings.tocmemex_llm_reasoning_effort:
        body["reasoning_effort"] = settings.tocmemex_llm_reasoning_effort
    if tools:
        body["tools"] = tools
    headers = {"Authorization": f"Bearer {settings.tocmemex_llm_api_key}"}
    url = settings.tocmemex_llm_base_url.rstrip("/") + "/chat/completions"

    from ..curation import llm_log

    tool_names = [tool.get("function", {}).get("name", "") for tool in tools or []]
    started = time.monotonic()
    attempts = max(1, retries + 1)
    last_error: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(attempts):
            try:
                response = client.post(url, json=body, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
            else:
                if response.status_code != 200:
                    last_error = LLMError(
                        f"LLM HTTP {response.status_code}: {response.text[:200]}"
                    )
                else:
                    try:
                        payload = response.json()
                        choices = payload.get("choices")
                        content = choices[0]["message"]["content"]
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        estimated = llm_log.estimate_tokens(prompt)
                        _log_call(
                            llm_log,
                            settings,
                            call_site=call_site,
                            run_id=run_id,
                            doc_id=doc_id,
                            prompt=prompt,
                            content="",
                            tokens={
                                "prompt": estimated,
                                "completion": 0,
                                "total": estimated,
                                "source": "estimated",
                            },
                            elapsed_ms=_elapsed_ms(started),
                            status="error",
                            error=f"missing choices: {exc}",
                            retries=attempt,
                            tool_names=tool_names,
                        )
                        raise LLMError(f"LLM response missing choices: {exc}") from exc
                    if content is None:
                        if not require_content:
                            _log_call(
                                llm_log,
                                settings,
                                call_site=call_site,
                                run_id=run_id,
                                doc_id=doc_id,
                                prompt=prompt,
                                content="",
                                tokens=llm_log.usage_from_response(payload, prompt, ""),
                                elapsed_ms=_elapsed_ms(started),
                                retries=attempt,
                                tool_names=tool_names,
                            )
                            return ""
                        _log_call(
                            llm_log,
                            settings,
                            call_site=call_site,
                            run_id=run_id,
                            doc_id=doc_id,
                            prompt=prompt,
                            content="",
                            tokens=llm_log.usage_from_response(payload, prompt, ""),
                            elapsed_ms=_elapsed_ms(started),
                            status="error",
                            error="content is null",
                            retries=attempt,
                            tool_names=tool_names,
                        )
                        raise LLMError("LLM response content is null")
                    _log_call(
                        llm_log,
                        settings,
                        call_site=call_site,
                        run_id=run_id,
                        doc_id=doc_id,
                        prompt=prompt,
                        content=content,
                        tokens=llm_log.usage_from_response(payload, prompt, content),
                        elapsed_ms=_elapsed_ms(started),
                        retries=attempt,
                        tool_names=tool_names,
                    )
                    return content
            if attempt < attempts - 1:
                time.sleep(_RETRY_DELAY_SECONDS)
    _log_call(
        llm_log,
        settings,
        call_site=call_site,
        run_id=run_id,
        doc_id=doc_id,
        prompt=prompt,
        content="",
        tokens={
            "prompt": llm_log.estimate_tokens(prompt),
            "completion": 0,
            "total": llm_log.estimate_tokens(prompt),
            "source": "estimated",
        },
        elapsed_ms=_elapsed_ms(started),
        status="error",
        error=str(last_error),
        retries=attempts - 1,
        tool_names=tool_names,
    )
    raise LLMError(f"LLM call failed after {attempts} attempts: {last_error}")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@dataclass
class ChatMessage:
    """One turn of a tool-calling conversation (OpenAI chat shape)."""

    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Each image is {"mime": str, "data_url": str}; bytes never go to logs.
    images: list[dict] | None = None

    def to_wire(self, *, drop_images: bool = False) -> dict:
        message: dict = {"role": self.role}
        if self.images and not drop_images:
            parts: list[dict] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for image in self.images:
                parts.append(
                    {"type": "image_url", "image_url": {"url": image["data_url"]}}
                )
            message["content"] = parts
        elif self.content is not None:
            content = self.content
            if self.images and drop_images:
                content = (content + "\n\n[图片输入被端点拒绝，已省略]").strip()
            message["content"] = content
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        if self.name:
            message["name"] = self.name
        return message


@dataclass
class ChatReply:
    content: str | None
    tool_calls: list[dict]
    tokens: dict
    images_dropped: bool = False
    # "length" means the endpoint stopped at max_tokens; tool-call arguments
    # are then usually cut mid-JSON and must not be executed.
    finish_reason: str | None = None


def llm_chat(
    messages: list[ChatMessage],
    settings: Settings | None = None,
    *,
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    call_site: str = "llm_chat",
    run_id: str | None = None,
    doc_id: str | None = None,
) -> ChatReply:
    """One tool-calling chat turn.

    Same endpoint contract as ``llm_complete``; the difference is the message
    list and that assistant ``tool_calls`` come back structured instead of
    being parsed out of prose. Usage and the full call are logged like any
    other call.
    """
    settings = settings or get_settings()
    max_tokens = settings.tocmemex_llm_max_tokens if max_tokens is None else max_tokens
    retries = settings.tocmemex_llm_retries if retries is None else retries
    timeout = settings.tocmemex_llm_timeout if timeout is None else timeout
    body: dict = {
        "model": settings.tocmemex_llm_model,
        "messages": [message.to_wire() for message in messages],
        "temperature": settings.tocmemex_llm_temperature,
        "max_tokens": max_tokens,
    }
    has_images = any(message.images for message in messages)
    images_dropped = False
    if tools:
        body["tools"] = tools
    if settings.tocmemex_llm_reasoning_effort:
        body["reasoning_effort"] = settings.tocmemex_llm_reasoning_effort
    headers = {"Authorization": f"Bearer {settings.tocmemex_llm_api_key}"}
    url = settings.tocmemex_llm_base_url.rstrip("/") + "/chat/completions"

    from ..curation import llm_log

    tool_names = [tool.get("function", {}).get("name", "") for tool in tools or []]
    prompt_text = "\n".join(
        (message.content or "") + (" [image]" if message.images else "")
        for message in messages
    )
    started = time.monotonic()
    attempts = max(1, retries + 1)
    last_error: Exception | None = None

    with httpx.Client(timeout=timeout) as client:
        for attempt in range(attempts):
            try:
                response = client.post(url, json=body, headers=headers)
                if (
                    response.status_code in (400, 413, 415, 422)
                    and has_images
                    and not images_dropped
                ):
                    # Some OpenAI-compatible endpoints reject image parts.
                    # Retry once without them so one figure cannot fail a run.
                    images_dropped = True
                    body["messages"] = [
                        message.to_wire(drop_images=True) for message in messages
                    ]
                    response = client.post(url, json=body, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
            else:
                if response.status_code != 200:
                    last_error = LLMError(
                        f"LLM HTTP {response.status_code}: {response.text[:200]}"
                    )
                else:
                    try:
                        payload = response.json()
                        choice = payload["choices"][0]
                        message = choice["message"]
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise LLMError(f"LLM response missing message: {exc}") from exc
                    content = message.get("content")
                    tool_calls = message.get("tool_calls") or []
                    finish_reason = choice.get("finish_reason")
                    tokens = llm_log.usage_from_response(
                        payload, prompt_text, content or ""
                    )
                    llm_log.append_call(
                        settings.tocmemex_data_dir,
                        call_site=call_site,
                        endpoint=settings.tocmemex_llm_base_url,
                        model=settings.tocmemex_llm_model,
                        prompt=prompt_text,
                        content=content,
                        tokens=tokens,
                        elapsed_ms=_elapsed_ms(started),
                        run_id=run_id,
                        doc_id=doc_id,
                        tools=tool_names,
                        tool_calls=[
                            {"name": call.get("function", {}).get("name"),
                             "args": call.get("function", {}).get("arguments")}
                            for call in tool_calls
                        ],
                        finish_reason=finish_reason,
                        retries=attempt,
                        log_mode=settings.tocmemex_llm_log,
                    )
                    return ChatReply(
                        content=content,
                        tool_calls=tool_calls,
                        tokens=tokens,
                        images_dropped=images_dropped,
                        finish_reason=finish_reason,
                    )
            if attempt < attempts - 1:
                time.sleep(_RETRY_DELAY_SECONDS)

    llm_log.append_call(
        settings.tocmemex_data_dir,
        call_site=call_site,
        endpoint=settings.tocmemex_llm_base_url,
        model=settings.tocmemex_llm_model,
        prompt=prompt_text,
        content=None,
        tokens={"prompt": llm_log.estimate_tokens(prompt_text), "completion": 0,
                "total": llm_log.estimate_tokens(prompt_text), "source": "estimated"},
        elapsed_ms=_elapsed_ms(started),
        status="error",
        error=str(last_error),
        retries=attempts - 1,
        run_id=run_id,
        doc_id=doc_id,
        tools=tool_names,
        log_mode=settings.tocmemex_llm_log,
    )
    raise LLMError(f"LLM chat failed after {attempts} attempts: {last_error}")


def _log_call(
    llm_log,
    settings: Settings,
    *,
    call_site: str,
    run_id: str | None,
    doc_id: str | None,
    prompt: str,
    content: str | None,
    tokens: dict,
    elapsed_ms: int,
    status: str = "ok",
    error: str | None = None,
    retries: int = 0,
    tool_names: list[str] | None = None,
) -> None:
    llm_log.append_call(
        settings.tocmemex_data_dir,
        call_site=call_site,
        endpoint=settings.tocmemex_llm_base_url,
        model=settings.tocmemex_llm_model,
        prompt=prompt,
        content=content,
        tokens=tokens,
        elapsed_ms=elapsed_ms,
        status=status,
        error=error,
        retries=retries,
        run_id=run_id,
        doc_id=doc_id,
        tools=tool_names,
        log_mode=settings.tocmemex_llm_log,
    )


def extract_json(content: str) -> Any:
    """Parse the JSON object/array out of an LLM response.

    Ported from PageIndex ``utils.py::extract_json``: strip ```json fences,
    normalize whitespace, parse; on failure retry after removing trailing
    commas. Raises ValueError instead of returning {} so callers can
    distinguish garbage from an empty list.
    """
    text = content.strip()
    start_idx = text.find("```json")
    if start_idx != -1:
        start_idx += 7
        end_idx = text.rfind("```")
        text = text[start_idx:end_idx if end_idx != -1 else None].strip()

    text = text.replace("None", "null")
    text = " ".join(text.replace("\n", " ").replace("\r", " ").split())
    # Prose around the payload ("前置说明 [...] 后置"): slice from the first
    # bracket to the last matching closer before parsing.
    first_array, first_object = text.find("["), text.find("{")
    starts = [i for i in (first_array, first_object) if i != -1]
    if starts:
        start = min(starts)
        closer = "]" if text[start] == "[" else "}"
        end = text.rfind(closer)
        if end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = text.replace(",]", "]").replace(",}", "}")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not JSON: {exc}") from exc
