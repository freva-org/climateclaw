import pytest

from climateclaw.services.streaming.stream_orchestrator import (
    StreamState,
    stream_with_tools,
)
from climateclaw.services.streaming.stream_variants import SVAssistant, SVStreamEnd


async def fake_assistant_stream():
    yield {
        "choices": [
            {
                "delta": {"content": "hello "},
                "finish_reason": None,
            }
        ]
    }
    yield {
        "choices": [
            {
                "delta": {"content": "world"},
                "finish_reason": "stop",
            }
        ]
    }


@pytest.mark.asyncio
async def test_stream_with_tools_consumes_async_iterator_response(
    patch_registry, patch_thread_storage
):
    patch_registry({"t-stream": []})

    async def fake_acomplete(**kwargs):
        assert kwargs["stream"] is True
        return fake_assistant_stream()

    items = [
        item
        async for item in stream_with_tools(
            model="test-model",
            thread_id="t-stream",
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert items == [
        SVAssistant(content="hello "),
        SVAssistant(content="world"),
        SVStreamEnd(content="Stream ended."),
    ]
