import asyncio
import json

import pytest

import climateclaw.services.streaming.active_conversations as act_conv
import climateclaw.services.streaming.stream_orchestrator as orchestrator
from climateclaw.services.streaming.active_conversations import Registry
from climateclaw.services.streaming.replay_gate import (
    REPLAY_DONE_STATUS,
    REPLAYING_CODE_STATUS,
    ReplayGate,
    ReplayProgress,
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

REPLAY_TOTAL = 3


def replaying_hint(current: int, total: int = REPLAY_TOTAL) -> SVServerHint:
    return SVServerHint(
        content={
            "busy": True,
            "detail": REPLAYING_CODE_STATUS,
            "progress": {"current": current, "total": total},
        }
    )


REPLAYING_HINT = replaying_hint(1)
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
    Registry[thread_id].replay_progress = ReplayProgress(total=REPLAY_TOTAL)
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


@pytest.mark.asyncio
async def test_replay_gate_reports_progress_until_done():
    release_replay = asyncio.Event()

    async def replay():
        await release_replay.wait()

    progress = ReplayProgress(total=REPLAY_TOTAL)
    gate = ReplayGate(asyncio.create_task(replay()), progress)

    assert gate.start_hint() == replaying_hint(1)
    # No change since the start hint -> nothing new to report
    assert gate.progress_hint_if_changed() is None
    assert gate.pending_hint_if_ready() is None

    hints = gate.wait_done_hints()
    next_hint = asyncio.create_task(anext(hints))
    await asyncio.sleep(0)
    assert not next_hint.done()

    progress.advance(2)
    assert await next_hint == replaying_hint(2)

    progress.advance(3)
    assert await anext(hints) == replaying_hint(3)

    release_replay.set()
    assert await anext(hints) == REPLAY_DONE_HINT
    with pytest.raises(StopAsyncIteration):
        await anext(hints)

    # Done hint is only sent once
    assert gate.pending_hint_if_ready() is None


@pytest.mark.asyncio
async def test_replay_gate_pending_hint_reports_progress_then_done():
    release_replay = asyncio.Event()

    async def replay():
        await release_replay.wait()

    progress = ReplayProgress(total=2)
    task = asyncio.create_task(replay())
    gate = ReplayGate(task, progress)

    assert gate.start_hint() == replaying_hint(1, 2)
    progress.advance(2)
    assert gate.pending_hint_if_ready() == replaying_hint(2, 2)
    assert gate.pending_hint_if_ready() is None

    release_replay.set()
    await task
    assert gate.pending_hint_if_ready() == REPLAY_DONE_HINT


@pytest.mark.asyncio
async def test_replay_gate_without_progress_uses_plain_status():
    async def replay():
        await asyncio.sleep(0)

    gate = ReplayGate(asyncio.create_task(replay()))

    assert gate.start_hint() == SVServerHint(
        content={"busy": True, "detail": REPLAYING_CODE_STATUS}
    )
    hints = [h async for h in gate.wait_done_hints()]
    assert hints == [REPLAY_DONE_HINT]


@pytest.mark.asyncio
async def test_replay_code_history_advances_progress(patch_registry, monkeypatch):
    patch_registry(
        {
            "t-progress": [
                {"variant": "Code", "content": f"print({i})", "id": f"c{i}"}
                for i in range(1, 4)
            ]
        }
    )
    Registry["t-progress"].mcp_manager = ToolMcpManager("code_interpreter")
    progress = ReplayProgress(total=3)
    seen_steps: list[int] = []

    async def fake_run_tool_via_mcp(**kwargs):
        seen_steps.append(progress.current)
        return "{}"

    monkeypatch.setattr(act_conv, "run_tool_via_mcp", fake_run_tool_via_mcp)

    await act_conv._replay_code_history("t-progress", progress)

    assert seen_steps == [1, 2, 3]
    assert progress.current == 3
