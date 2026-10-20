from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from climateclaw.core.logging_setup import REQUEST_ID_HEADER, get_request_id
from climateclaw.core.settings import get_settings

# ──── Settings ──────────────────────────────────────────────────────────────────

# Optional bearer to satisfy proxies that require it.
OPENAI_API_KEY = os.getenv("CLIMATECLAW_OPENAI_API_KEY", "")

# ──── Helpers ───────────────────────────────────────────────────────────────────


def _api_url(endpoint: str) -> str:
    """Build a LiteLLM API URL for the given endpoint."""
    s = get_settings()
    return f"{s.LITE_LLM_ADDRESS.rstrip('/')}/{endpoint.lstrip('/')}"


def _is_responses_endpoint(endpoint: str) -> bool:
    """Return whether the endpoint targets the Responses API."""
    return endpoint.strip("/").endswith("responses")


def _headers() -> dict[str, str]:
    """Build headers for a LiteLLM request."""
    h = {"Content-Type": "application/json"}
    request_id = get_request_id()
    if request_id:
        h[REQUEST_ID_HEADER] = request_id

    # Authorization header is not required for local models,
    # but sending it (when available) doesn’t hurt and satisfies OpenAI-routed calls.
    if OPENAI_API_KEY:
        h["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    return h


async def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Streaming responses have not read their body when headers arrive.
        await response.aread()

        try:
            body = response.json()
            error = body.get("error", body)

            if isinstance(error, dict):
                detail = error.get("message") or error.get("detail")
            else:
                detail = str(error)

            detail = detail or response.text
        except (ValueError, AttributeError):
            detail = response.text

        detail = str(detail).strip() or "No error details returned"

        raise httpx.HTTPStatusError(
            f"LiteLLM returned HTTP {response.status_code}: {detail}",
            request=exc.request,
            response=response,
        ) from exc


async def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Post a JSON payload and return the decoded response."""
    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=_headers())
        await _raise_for_status(r)
        return r.json()


def _extract_text(resp: Any) -> str:
    """Extract assistant text from either supported response format."""
    if not isinstance(resp, dict):
        return ""

    # Chat Completions responses are identified by the top-level `choices` field.
    # Assistant text (or a refusal) lives in the first choice's `message`.
    if "choices" in resp:
        choices = resp.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return ""

        # `n > 1` can produce alternative choices, but ClimateClaw follows one
        # conversation path and therefore intentionally uses only the first.
        choice = choices[0]
        if not isinstance(choice, dict):
            return ""

        message = choice.get("message") or {}
        if not isinstance(message, dict):
            return ""

        content = message.get("content")
        if isinstance(content, str):
            return content

        refusal = message.get("refusal")
        return refusal if isinstance(refusal, str) else ""

    # Responses API payloads are identified by `output` or the `output_text` fields.
    if "output" not in resp and "output_text" not in resp:
        return ""

    output_text = resp.get("output_text")
    if isinstance(output_text, str):
        return output_text

    text_parts: list[str] = []
    for item in resp.get("output") or []:
        # Ignore non-message items such as reasoning and function calls
        if not isinstance(item, dict) or item.get("type") != "message":
            continue

        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue

            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
            elif content.get("type") == "refusal":
                text = content.get("refusal")
            else:
                continue

            if isinstance(text, str):
                text_parts.append(text)

    return "".join(text_parts)


# ──── Public API - supports v1/chat/completions and v1/responses ────────────────


async def acomplete(
    *,
    model: str,
    endpoint: str = "v1/chat/completions",
    messages: Iterable[dict[str, Any]],
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_params: dict[str, Any] | None = None,
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

    is_responses_api = _is_responses_endpoint(endpoint)

    if is_responses_api:
        payload["input"] = list(messages)
    else:
        payload["messages"] = list(messages)

    if temperature is not None:
        payload["temperature"] = temperature

    if max_tokens is not None:
        if is_responses_api:
            payload["max_output_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

    if extra_params:
        payload.update(extra_params)

    request_id = get_request_id()
    metadata = payload.get("metadata")
    if request_id:
        if isinstance(metadata, dict):
            payload["metadata"] = {**metadata, "request_id": request_id}
        else:
            payload["metadata"] = {"request_id": request_id}

    if not stream:
        return await _post_json(url, payload)

    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    client = httpx.AsyncClient(timeout=timeout)

    async def _aiter() -> AsyncIterator[dict[str, Any]]:
        try:
            async with client.stream(
                "POST", url, json=payload, headers=_headers()
            ) as r:
                await _raise_for_status(r)
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
    """Return the first assistant text from a completion response."""
    return _extract_text(resp)


def tool_calls(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize function calls from Chat Completions or Responses payloads."""
    if not isinstance(resp, dict):
        return []

    # Chat Completions responses are identified by `choices`; function calls are
    # already nested under the first choice's assistant message.
    if "choices" in resp:
        choices = resp.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return []

        # `n > 1` can produce alternative choices, but ClimateClaw follows one
        # conversation path and must not combine calls from competing choices.
        choice = choices[0]
        if not isinstance(choice, dict):
            return []

        message = choice.get("message") or {}
        if not isinstance(message, dict):
            return []

        chat_calls = message.get("tool_calls") or []
        if not isinstance(chat_calls, list):
            return []

        return [call for call in chat_calls if isinstance(call, dict)]

    # Responses API payloads use typed `output` items. Only `function_call`
    # items are converted here; built-in tool items are intentionally ignored.
    if "output" not in resp:
        return []

    response_calls: list[dict[str, Any]] = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue

        name = item.get("name")
        if not name:
            continue

        arguments = item.get("arguments")
        response_calls.append(
            {
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments if arguments is not None else "",
                },
            }
        )

    return response_calls


def first_message(resp: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first assistant message, if present."""
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
