from unittest.mock import patch

import pytest
import requests

from climateclaw.services.streaming.litellm_client import (
    acomplete,
    first_message,
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
            # mimic requests behavior: raise HTTPError and attach response
            e = requests.HTTPError(f"{self.status_code} Server Error")
            e.response = self  # tests / client code can read e.response.text/json()
            raise e


@pytest.mark.asyncio
async def test_acomplete_success_roundtrip(monkeypatch):
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
async def test_acomplete_responses_prefers_explicit_input():
    fake = FakeResp(status_code=200, json_body={"output_text": "ok"}, text="")
    captured = {}

    async def fake_post(self, *args, **kwargs):
        captured["json"] = kwargs["json"]
        return fake

    with patch(
        "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
        new=fake_post,
    ):
        await acomplete(
            model="gpt-4.1-mini",
            endpoint="v1/responses",
            messages=[{"role": "user", "content": "ignored"}],
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "used"}],
                }
            ],
        )

    assert captured["json"]["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "used"}],
        }
    ]


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
    assert first_message(resp) == {
        "role": "assistant",
        "content": "hello world",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "lookup_reference",
                    "arguments": '{"topic":"ice"}',
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_acomplete_includes_error_body(monkeypatch):
    fake = FakeResp(
        status_code=500,
        json_body={"error": {"message": "bad"}},
        text='{"error":"bad"}',
    )

    async def fake_post(self, *args, **kwargs):
        return fake

    with (
        patch(
            "climateclaw.services.streaming.litellm_client.httpx.AsyncClient.post",
            new=fake_post,
        ),
        pytest.raises(requests.HTTPError) as ei,
    ):
        await acomplete(model="x", messages=[])

    assert "500 Server Error" in str(ei.value)
    assert ei.value.response is not None
    assert "bad" in (ei.value.response.text or "")
