import asyncio

import pytest

from climateclaw.services.streaming import stream_orchestrator
from climateclaw.services.streaming.stream_variants import SVServerHint


@pytest.mark.asyncio
async def test_yield_heartbeats_until_emits_after_quiet_period(monkeypatch):
    async def fake_heartbeat_content():
        return SVServerHint(content={"heartbeat": True})

    async def slow_task():
        await asyncio.sleep(0.03)
        return "done"

    monkeypatch.setattr(
        stream_orchestrator, "heartbeat_content", fake_heartbeat_content
    )

    task = asyncio.create_task(slow_task())
    heartbeats = [
        item
        async for item in stream_orchestrator._yield_heartbeats_until(
            task, interval=0.01
        )
    ]

    assert await task == "done"
    assert heartbeats
    assert all(isinstance(item, SVServerHint) for item in heartbeats)


@pytest.mark.asyncio
async def test_yield_heartbeats_until_is_quiet_for_fast_task(monkeypatch):
    async def fake_heartbeat_content():
        return SVServerHint(data={"heartbeat": True})

    async def fast_task():
        return "done"

    monkeypatch.setattr(
        stream_orchestrator, "heartbeat_content", fake_heartbeat_content
    )

    task = asyncio.create_task(fast_task())
    heartbeats = [
        item
        async for item in stream_orchestrator._yield_heartbeats_until(
            task, interval=0.01
        )
    ]

    assert await task == "done"
    assert heartbeats == []
