import asyncio
import json

import pytest

import climateclaw.services.streaming.stream_orchestrator as orchestrator
from climateclaw.services.streaming.active_conversations import Registry
from climateclaw.services.streaming.replay_gate import (
    REPLAY_DONE_STATUS,
    REPLAYING_CODE_STATUS,
)
from climateclaw.services.streaming.stream_orchestrator import (
    StreamState,
    stream_with_tools,
)
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVServerHint,
    SVStreamEnd,
)
from conftest import DummyMcpManager

REPLAYING_HINT = SVServerHint(content={"busy": True, "detail": REPLAYING_CODE_STATUS})
REPLAY_DONE_HINT = SVServerHint(content={"busy": False, "detail": REPLAY_DONE_STATUS})


class ToolMcpManager(DummyMcpManager):
    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def _tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "description": f"Run {self.tool_name}",
                    "parameters": {"type": "object"},
                },
            }
        ]

    async def openai_tools(self):
        return self._tools()

    async def available_tools(self):
        return self._tools()


async def fake_code_interpreter_stream():
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_code",
                            "function": {
                                "name": "code_interpreter",
                                "arguments": '{"code": "print(2)"}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


async def fake_web_search_stream():
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_web",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "climate"}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


async def fake_plain_text_stream():
    yield {
        "choices": [
            {
                "delta": {"content": "plain answer"},
                "finish_reason": "stop",
            }
        ]
    }


def set_replay_conversation(patch_registry, thread_id: str, tool_name: str):
    release_replay = asyncio.Event()

    async def replay():
        await release_replay.wait()

    replay_task = asyncio.create_task(replay())
    patch_registry({thread_id: []})
    Registry[thread_id].mcp_manager = ToolMcpManager(tool_name)
    Registry[thread_id].replay_task = replay_task
    return release_replay, replay_task


@pytest.mark.asyncio
async def test_code_interpreter_waits_for_replay_before_mcp_call(
    patch_registry, patch_thread_storage, monkeypatch
):
    release_replay, _ = set_replay_conversation(
        patch_registry, "t-code", "code_interpreter"
    )
    mcp_called = asyncio.Event()

    async def fake_acomplete(**kwargs):
        return fake_code_interpreter_stream()

    async def fake_run_tool_via_mcp(**kwargs):
        mcp_called.set()
        return json.dumps(
            {
                "structuredContent": {
                    "stdout": "2\n",
                    "stderr": "",
                    "display_data": [],
                }
            }
        )

    monkeypatch.setattr(orchestrator, "run_tool_via_mcp", fake_run_tool_via_mcp)

    stream = stream_with_tools(
        model="test-model",
        thread_id="t-code",
        messages=[],
        acomplete_func=fake_acomplete,
        stream_state=StreamState(),
        storage=patch_thread_storage,
        store_thread=False,
    )

    assert await anext(stream) == REPLAYING_HINT
    assert isinstance(await anext(stream), SVCode)

    waiting_for_replay = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    assert not waiting_for_replay.done()
    assert not mcp_called.is_set()

    release_replay.set()

    assert await waiting_for_replay == REPLAY_DONE_HINT

    seen_code_output = False
    async for item in stream:
        if isinstance(item, SVCodeOutput):
            seen_code_output = True
            break

    assert seen_code_output
    assert mcp_called.is_set()


@pytest.mark.asyncio
async def test_plain_answer_sends_replay_hints_without_blocking_answer(
    patch_registry, patch_thread_storage
):
    release_replay, _ = set_replay_conversation(
        patch_registry, "t-plain", "code_interpreter"
    )

    async def fake_acomplete(**kwargs):
        return fake_plain_text_stream()

    stream = stream_with_tools(
        model="test-model",
        thread_id="t-plain",
        messages=[],
        acomplete_func=fake_acomplete,
        stream_state=StreamState(),
        storage=patch_thread_storage,
        store_thread=False,
    )

    assert await anext(stream) == REPLAYING_HINT
    assert await anext(stream) == SVAssistant(content="plain answer")

    waiting_for_done = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    assert not waiting_for_done.done()

    release_replay.set()

    assert await waiting_for_done == REPLAY_DONE_HINT
    assert isinstance(await anext(stream), SVStreamEnd)


@pytest.mark.asyncio
async def test_non_code_tool_does_not_wait_but_emits_done_when_replay_finishes(
    patch_registry, patch_thread_storage, monkeypatch
):
    release_replay, replay_task = set_replay_conversation(
        patch_registry, "t-web", "web_search"
    )
    mcp_called = asyncio.Event()
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    async def fake_acomplete(**kwargs):
        return fake_web_search_stream()

    async def fake_run_tool_via_mcp(**kwargs):
        mcp_called.set()
        release_replay.set()
        await replay_task
        return json.dumps({"structuredContent": {"result": "search result"}})

    monkeypatch.setattr(orchestrator, "run_tool_via_mcp", fake_run_tool_via_mcp)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    stream = stream_with_tools(
        model="test-model",
        thread_id="t-web",
        messages=[],
        acomplete_func=fake_acomplete,
        stream_state=StreamState(),
        storage=patch_thread_storage,
        store_thread=False,
    )

    assert await anext(stream) == REPLAYING_HINT
    tool_call = await anext(stream)
    assert tool_call.variant == "ToolCall"

    done_hint = None
    async for item in stream:
        if item == REPLAY_DONE_HINT:
            done_hint = item
            break

    assert mcp_called.is_set()
    assert done_hint == REPLAY_DONE_HINT
