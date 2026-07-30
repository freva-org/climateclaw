import pytest


@pytest.mark.asyncio
async def test_streamresponse_returns_500_on_prepare_failure(
    stub_resp,
    client,
    GOOD_HEADERS,
    monkeypatch,
):
    async def _raise_error(**kwargs):
        raise RuntimeError("prep failed")

    monkeypatch.setattr(
        "climateclaw.api.chatbot.streamresponse.prepare_for_stream",
        _raise_error,
        raising=True,
    )

    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/streamresponse",
                json={"thread_id": "t-err", "input": "hi", "user_id": "alice"},
                headers={**GOOD_HEADERS, "x-freva-config-path": "/tmp/config.yml"},
            )

            assert r.status_code == 500
            assert "Internal Server Error" in r.json()["detail"]
