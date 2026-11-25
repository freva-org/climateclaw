import httpx
import pytest

from climateclaw.core import available_chatbots as chatbots


def _proxy_raises(monkeypatch, exc: Exception) -> None:
    async def list_models() -> list[str]:
        raise exc

    monkeypatch.setattr(chatbots, "list_models", list_models)


def _proxy_lists_nothing(monkeypatch) -> None:
    async def list_models() -> list[str]:
        return []

    monkeypatch.setattr(chatbots, "list_models", list_models)


@pytest.mark.asyncio
async def test_availablechatbots_503_when_litellm_lists_no_models(
    stub_resp, client, GOOD_HEADERS, monkeypatch
):
    _proxy_lists_nothing(monkeypatch)
    with stub_resp:
        async with client:
            r = await client.get("/api/chatbot/availablechatbots", headers=GOOD_HEADERS)
    assert r.status_code == 503
    assert r.json()["detail"] == "No chatbots available: LiteLLM lists no models."


@pytest.mark.asyncio
async def test_availablechatbots_502_when_litellm_is_unreachable(
    stub_resp, client, GOOD_HEADERS, monkeypatch
):
    _proxy_raises(monkeypatch, httpx.ConnectError("connection refused"))
    with stub_resp:
        async with client:
            r = await client.get("/api/chatbot/availablechatbots", headers=GOOD_HEADERS)
    assert r.status_code == 502
    assert r.json()["detail"].startswith(
        "Could not get the available chatbots from LiteLLM:"
    )


@pytest.mark.asyncio
async def test_streamresponse_503_when_litellm_lists_no_models(
    stub_resp, client, GOOD_HEADERS, monkeypatch
):
    _proxy_lists_nothing(monkeypatch)
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/streamresponse",
                json={"thread_id": "t-1", "input": "hi"},
                headers=GOOD_HEADERS,
            )
    assert r.status_code == 503
    assert r.json()["detail"] == "No chatbots available: LiteLLM lists no models."


@pytest.mark.asyncio
async def test_streamresponse_502_when_litellm_is_unreachable(
    stub_resp, client, GOOD_HEADERS, monkeypatch
):
    _proxy_raises(monkeypatch, httpx.ConnectError("connection refused"))
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/streamresponse",
                json={"thread_id": "t-1", "input": "hi"},
                headers=GOOD_HEADERS,
            )
    assert r.status_code == 502
