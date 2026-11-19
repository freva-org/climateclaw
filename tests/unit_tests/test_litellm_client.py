from unittest.mock import patch

import httpx
import pytest

from climateclaw.services.streaming.litellm_client import (
    acomplete,
    first_text,
    tool_calls,
)


class FakeResp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("No JSON")
        return self._json

    def raise_for_status(self):
        if not self.ok:
            request = httpx.Request("POST", "http://test")
            response = httpx.Response(
                self.status_code,
                request=request,
                text=self.text,
            )
            raise httpx.HTTPStatusError(
                f"{self.status_code} Server Error",
                request=request,
                response=response,
            )


@pytest.mark.asyncio
async def test_acomplete_success_roundtrip():
    fake = FakeResp(
        status_code=200,
        json_body={"choices": [{"message": {"content": "hello world"}}]},
        text='{"choices":[{"message":{"content":"hello world"}}]}',
    )

    async def fake_post(self, *args, **kwargs):
        return fake

    with patch(
        "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
        new=fake_post,
    ):
        result = await acomplete(
            model="qwen2.5:3b", messages=[{"role": "user", "content": "hi"}]
        )

    assert first_text(result) == "hello world"


@pytest.mark.asyncio
async def test_acomplete_responses_accepts_messages_and_translates_payload():
    fake = FakeResp(
        status_code=200,
        json_body={"output_text": "hello from responses"},
        text='{"output_text":"hello from responses"}',
    )
    captured = {}

    async def fake_post(self, *args, **kwargs):
        captured["url"] = args[0]
        captured["json"] = kwargs["json"]
        return fake

    with patch(
        "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
        new=fake_post,
    ):
        result = await acomplete(
            model="gpt-4.1-mini",
            endpoint="/v1/responses",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=25,
            temperature=0.2,
        )

    assert captured["url"].endswith("/v1/responses")
    assert captured["json"] == {
        "model": "gpt-4.1-mini",
        "stream": False,
        "input": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "max_output_tokens": 25,
    }
    assert first_text(result) == "hello from responses"


@pytest.mark.asyncio
async def test_acomplete_includes_tool_configuration_when_tools_are_provided():
    fake = FakeResp(status_code=200, json_body={})
    captured = {}
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup_reference", "parameters": {}},
        }
    ]

    async def fake_post(self, *args, **kwargs):
        captured["json"] = kwargs["json"]
        return fake

    with patch(
        "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
        new=fake_post,
    ):
        await acomplete(
            model="test-model",
            messages=[],
            tools=tools,
            tool_choice="required",
        )

    assert captured["json"]["tools"] == tools
    assert captured["json"]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_acomplete_omits_tool_configuration_when_tools_are_empty():
    fake = FakeResp(status_code=200, json_body={})
    captured = {}

    async def fake_post(self, *args, **kwargs):
        captured["json"] = kwargs["json"]
        return fake

    with patch(
        "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
        new=fake_post,
    ):
        await acomplete(
            model="test-model",
            messages=[],
            tools=[],
            tool_choice="required",
        )

    assert "tools" not in captured["json"]
    assert "tool_choice" not in captured["json"]


@pytest.mark.asyncio
async def test_acomplete_includes_error_body():
    request = httpx.Request("POST", "http://litellm/v1/chat/completions")
    fake = httpx.Response(
        status_code=500,
        json={"error": {"message": "bad"}},
        request=request,
    )

    async def fake_post(self, *args, **kwargs):
        return fake

    with (
        patch(
            "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
            new=fake_post,
        ),
        pytest.raises(httpx.HTTPStatusError) as ei,
    ):
        await acomplete(model="x", messages=[])

    assert "LiteLLM returned HTTP 500" in str(ei.value)
    assert "bad" in str(ei.value)
    assert ei.value.response is not None
    assert "bad" in (ei.value.response.text or "")


def test_responses_helpers_extract_text_message_and_tool_calls():
    resp = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "hello "},
                    {"type": "output_text", "text": "world"},
                ],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup_reference",
                "arguments": '{"topic":"ice"}',
            },
        ]
    }

    assert first_text(resp) == "hello world"
    assert tool_calls(resp) == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "lookup_reference",
                "arguments": '{"topic":"ice"}',
            },
        }
    ]


def test_chat_helpers_extract_text_refusal_and_tool_calls():
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "lookup_reference",
                "arguments": '{"topic":"ice"}',
            },
        }
    ]
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "refusal": "I cannot help with that request.",
                    "tool_calls": calls,
                }
            }
        ]
    }

    assert first_text(resp) == "I cannot help with that request."
    assert tool_calls(resp) == calls


def test_responses_helpers_extract_refusal():
    resp = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "refusal",
                        "refusal": "I cannot help with that request.",
                    }
                ],
            }
        ]
    }

    assert first_text(resp) == "I cannot help with that request."
    assert tool_calls(resp) == []


@pytest.mark.parametrize(
    "resp", [None, [], {}, {"choices": [None]}, {"output": [None]}]
)
def test_helpers_return_empty_defaults_for_malformed_responses(resp):
    assert first_text(resp) == ""
    assert tool_calls(resp) == []


@pytest.mark.parametrize(
    ("litellm_key", "openai_key", "expected"),
    [
        ("sk-litellm", "sk-openai", "Bearer sk-litellm"),
        ("", "sk-openai", "Bearer sk-openai"),
        ("", "", None),
    ],
    ids=["litellm key first", "then openai key", "no key"],
)
def test_headers_key_precedence(monkeypatch, litellm_key, openai_key, expected):
    from climateclaw.services.streaming import litellm_client

    monkeypatch.setattr(litellm_client, "LITELLM_API_KEY", litellm_key)
    monkeypatch.setattr(litellm_client, "OPENAI_API_KEY", openai_key)

    assert litellm_client._headers().get("Authorization") == expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CLIMATECLAW_LITELLM_API_KEY": "sk-a"}, "sk-a"),
        ({"CLIMATECLAW_LITE_LLM_API_KEY": "sk-b"}, "sk-b"),
        (
            {
                "CLIMATECLAW_LITELLM_API_KEY": "sk-a",
                "CLIMATECLAW_LITE_LLM_API_KEY": "sk-b",
            },
            "sk-a",
        ),
        ({}, ""),
    ],
    ids=["LITELLM", "LITE_LLM", "LITELLM wins", "neither"],
)
def test_api_key_reads_either_spelling(monkeypatch, env, expected):
    from climateclaw.services.streaming import litellm_client

    for name in ("CLIMATECLAW_LITELLM_API_KEY", "CLIMATECLAW_LITE_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert litellm_client._getenv_litellm("API_KEY") == expected


@pytest.mark.asyncio
async def test_list_models_returns_ids_in_proxy_order(monkeypatch):
    from climateclaw.services.streaming import litellm_client

    monkeypatch.setattr(litellm_client, "LITELLM_API_KEY", "sk-litellm")
    captured = {}

    async def fake_get(self, url, headers):
        captured["url"] = url
        captured["auth"] = headers.get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"id": "model-b"}, {"id": "model-a"}, {"object": "model"}]},
            request=httpx.Request("GET", url),
        )

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        models = await litellm_client.list_models()

    assert models == ["model-b", "model-a"]
    assert captured["url"].endswith("/v1/models")
    assert captured["auth"] == "Bearer sk-litellm"


@pytest.mark.asyncio
async def test_list_models_raises_when_the_proxy_rejects_the_key():
    from climateclaw.services.streaming import litellm_client

    async def fake_get(self, url, headers):
        return httpx.Response(
            401,
            json={"error": {"message": "Invalid proxy server token passed"}},
            request=httpx.Request("GET", url),
        )

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        with pytest.raises(httpx.HTTPStatusError, match="HTTP 401"):
            await litellm_client.list_models()
