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
