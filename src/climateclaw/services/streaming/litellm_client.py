from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from climateclaw.core.settings import get_settings

# ──── Settings ───────────────────────────────────────────────────────────────────

# Optional bearer to satisfy proxies that require it.
AUTH_TOKEN = os.getenv("CLIMATECLAW_OPENAI_API_KEY", "")


def _api_url(endpoint: str) -> str:
    s = get_settings()
    return f"{s.LITE_LLM_ADDRESS.rstrip('/')}/{endpoint.lstrip('/')}"


def _is_responses_endpoint(endpoint: str) -> bool:
    return endpoint.strip("/").endswith("responses")


def _passthrough_params(params: dict[str, Any] | None) -> dict[str, Any]:
    # Tiny wrapper to allow future param sanitization
    return dict(params or {})


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    # Authorization header is not required for Ollama models,
    # but sending it (when available) doesn’t hurt and satisfies OpenAI-routed calls.
    if AUTH_TOKEN:
        h["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return h


async def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=_headers())
        r.raise_for_status()
        return r.json()


def _extract_text(resp: Any) -> str:
    # Chat Completions API
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass

    # Responses API
    try:
        output_text = resp.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        output = resp.get("output", [])
        text_parts: list[str] = []

        for item in output:
            if item.get("type") != "message":
                continue

            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if text:
                        text_parts.append(text)

        return "".join(text_parts)
    except (AttributeError, TypeError):
        return ""


# ──── Public API - supports v1/chat/completions and v1/responses ────────────────────────


async def acomplete(
    *,
    model: str,
    endpoint: str = "v1/chat/completions",
    messages: Iterable[dict[str, Any]],
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra: dict[str, Any] | None = None,
    **request_params: Any,
) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
    """
    Call LiteLLM v1/chat/completions or v1/responses.
    - stream=False: return JSON dict
    - stream=True: return **async iterator** yielding OpenAI-style stream chunks (dicts)
    """
    url = _api_url(endpoint)

    payload: dict[str, Any] = {
        "model": model,
        "stream": stream,
    }

    is_responses = _is_responses_endpoint(endpoint)

    if is_responses:
        payload["input"] = list(messages)
    else:
        payload["messages"] = list(messages)

    if temperature is not None:
        payload["temperature"] = temperature

    if max_tokens is not None:
        if is_responses:
            payload["max_output_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

    if extra:
        payload.update(extra)

    if request_params:
        payload.update(_passthrough_params(request_params))

    if not stream:
        return await _post_json(url, payload)

    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    client = httpx.AsyncClient(timeout=timeout)

    async def _aiter() -> AsyncIterator[dict[str, Any]]:
        try:
            async with client.stream(
                "POST", url, json=payload, headers=_headers()
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
        finally:
            await client.aclose()

    return _aiter()


def first_text(resp: Any) -> str:
    return _extract_text(resp)


def tool_calls(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize tool/function-calls from chat completions and Responses API
    payloads. Returns [] if absent.
    """
    try:
        choices = resp.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            tc = msg.get("tool_calls") or []
            if isinstance(tc, list):
                return [t for t in tc if isinstance(t, dict)]

        calls: list[dict[str, Any]] = []
        for item in resp.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = item.get("name")
            arguments = item.get("arguments")
            if not name:
                continue
            calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments if arguments is not None else "",
                    },
                }
            )
        return calls
    except Exception:
        return []


def first_message(resp: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convenience: return the first assistant message dict or None.
    """
    try:
        choices = resp.get("choices") or []
        if choices:
            return choices[0].get("message")

        if "output" not in resp and "output_text" not in resp:
            return None

        return {
            "role": "assistant",
            "content": first_text(resp),
            "tool_calls": tool_calls(resp),
        }
    except Exception:
        return None


__all__ = [
    "acomplete",
    "first_message",
    "first_text",
    "tool_calls",
]
