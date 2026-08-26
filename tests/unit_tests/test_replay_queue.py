import asyncio
import json

import pytest

import climateclaw.services.streaming.stream_orchestrator as orchestrator
from climateclaw.services.streaming.active_conversations import (
    Registry,
    wait_for_replay_if_needed,
)
from climateclaw.services.streaming.stream_orchestrator import (
    StreamState,
    stream_with_tools,
)
from climateclaw.services.streaming.stream_variants import (
    SVCode,
    SVCodeOutput,
    SVServerHint,
)
from conftest import DummyMcpManager


class CodeToolMcpManager(DummyMcpManager):
    async def openai_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "code_interpreter",
                    "description": "Execute Python code",
                    "parameters": {"type": "object"},
                },
            }
        ]


async def fake_code_interpreter_stream():
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
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


@pytest.mark.asyncio
async def test_wait_for_replay_if_needed_yields_status_hints(patch_registry):
    release_replay = asyncio.Event()

    async def replay():
        await release_replay.wait()

    task = asyncio.create_task(replay())
    patch_registry({"t-replay": []})
    Registry["t-replay"].replay_task = task

    hints = []

    async def collect_hints():
        async for hint in wait_for_replay_if_needed("t-replay"):
            hints.append(hint)

    waiter = asyncio.create_task(collect_hints())
    await asyncio.sleep(0)

    assert hints == [SVServerHint(data={"status": "Executing previous code blocks..."})]
    assert not waiter.done()

    release_replay.set()
    await waiter

    assert hints == [
        SVServerHint(data={"status": "Executing previous code blocks..."}),
        SVServerHint(data={"status": "Execution of previous code blocks is done."}),
    ]


@pytest.mark.asyncio
async def test_code_interpreter_waits_for_replay_before_mcp_call(
    patch_registry, patch_thread_storage, monkeypatch
):
    release_replay = asyncio.Event()
    mcp_called = asyncio.Event()

    async def replay():
        await release_replay.wait()

    replay_task = asyncio.create_task(replay())
    patch_registry({"t-stream": []})
    Registry["t-stream"].mcp_manager = CodeToolMcpManager()
    Registry["t-stream"].replay_task = replay_task

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
        thread_id="t-stream",
        messages=[],
        acomplete_func=fake_acomplete,
        stream_state=StreamState(),
        storage=patch_thread_storage,
        store_thread=False,
    )

    first = await anext(stream)
    second = await anext(stream)

    assert isinstance(first, SVCode)
    assert second == SVServerHint(data={"status": "Executing previous code blocks..."})
    assert not mcp_called.is_set()

    release_replay.set()

    seen_done_hint = False
    seen_code_output = False
    async for item in stream:
        if item == SVServerHint(
            data={"status": "Execution of previous code blocks is done."}
        ):
            seen_done_hint = True
        if isinstance(item, SVCodeOutput):
            seen_code_output = True
            break

    assert seen_done_hint
    assert seen_code_output
    assert mcp_called.is_set()
