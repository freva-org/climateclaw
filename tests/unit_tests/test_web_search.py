import httpx
import pytest

import climateclaw.tools.web_search.server as ws

UPSTREAM_401 = (
    "LiteLLM returned HTTP 401: litellm.AuthenticationError: AuthenticationError: "
    "OpenAIException - Incorrect API key provided: sk-proj-****4DCj."
)


@pytest.fixture
def failing_completion(monkeypatch):
    monkeypatch.setattr(ws, "current_ids", lambda: ("sid-1", "rid-1"))

    def _fail_with(status: int, message: str):
        async def fake_acomplete(**kwargs):
            request = httpx.Request("POST", "http://litellm:4000/v1/responses")
            response = httpx.Response(status, request=request)
            raise httpx.HTTPStatusError(message, request=request, response=response)

        monkeypatch.setattr(ws, "acomplete", fake_acomplete)

    return _fail_with


@pytest.fixture
def request_id():
    from climateclaw.core.logging_setup import reset_request_id, set_request_id

    token = set_request_id("req-123")
    yield "req-123"
    reset_request_id(token)


def assert_generic_error(result, request_id):
    assert result.result == ""
    assert "Web search is unavailable" in result.error
    assert request_id in result.error
    # No status, config details, upstream noise or key fragments reach the user.
    for leaked in ("HTTP", "API key", "CLIMATECLAW_", "sk-", "OpenAIException"):
        assert leaked not in result.error


@pytest.mark.parametrize("status", [401, 403])
async def test_invalid_key_returns_generic_error(
    failing_completion, request_id, status, caplog
):
    failing_completion(status, UPSTREAM_401)

    result = await ws.web_search("How do I submit a batch job on DKRZ HPC?")

    assert_generic_error(result, request_id)
    # Operators find the details and the config hint in the server log instead.
    assert "Incorrect API key" in caplog.text
    assert "CLIMATECLAW_OPENAI_API_KEY" in caplog.text
    assert "CLIMATECLAW_LITELLM_MASTER_KEY" in caplog.text


async def test_other_errors_return_generic_error(
    failing_completion, request_id, caplog
):
    failing_completion(500, "LiteLLM returned HTTP 500: boom")

    result = await ws.web_search("query")

    assert_generic_error(result, request_id)
    assert "boom" in caplog.text
