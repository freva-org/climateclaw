import pytest


@pytest.mark.asyncio
async def test_getthread_requires_thread_id(stub_resp, client, GOOD_HEADERS):
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/getthread", json={}, headers=GOOD_HEADERS
            )
            assert r.status_code == 422
            assert (
                r.json()["detail"]
                == "Thread ID not found. Please provide thread_id in the query parameters."
            )


@pytest.mark.asyncio
async def test_getthread_ok_with_thread_id(
    stub_resp, client, patch_read_thread, GOOD_HEADERS
):
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/getthread",
                json={"thread_id": "t-123"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 200
            body = r.json()
            # Prompt should be filtered out by the route
            assert isinstance(body, list)
            variants = [item.get("variant") for item in body]
            assert "Prompt" not in variants
            assert "User" in variants and "Assistant" in variants


@pytest.mark.asyncio
async def test_streamresponse_accepts_params_and_headers(
    stub_resp,
    client,
    patch_stream,
    patch_read_thread,
    patch_mcp_manager,
    GOOD_HEADERS,
):
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/streamresponse",
                json={"thread_id": "t-999", "input": "hello", "user_id": "alice"},
                headers={**GOOD_HEADERS},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("application/x-ndjson")
            # Optional: the body should look like SSE (contains 'event:' lines)
            text = r.text
            assert "ServerHint" in text
            assert "Assistant" in text


@pytest.mark.asyncio
async def test_stop_requires_thread_id(stub_resp, client, GOOD_HEADERS):
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/stop",
                json={},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 422
            assert (
                r.json()["detail"]
                == "Thread ID is missing. Please provide a thread_id in the request body."
            )


@pytest.mark.asyncio
async def test_stop_returns_404_for_unknown_thread(stub_resp, client, GOOD_HEADERS):
    with stub_resp:
        async with client:
            r = await client.post(
                "/api/chatbot/stop",
                json={"thread_id": "missing-thread"},
                headers=GOOD_HEADERS,
            )
            assert r.status_code == 404
            assert (
                r.json()["detail"]
                == "Conversation with given thread-id not found in the registry: missing-thread"
            )
