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


@pytest.mark.parametrize("status", [401, 403])
async def test_invalid_key_returns_clear_error(failing_completion, status, caplog):
    failing_completion(status, UPSTREAM_401)

    result = await ws.web_search("How do I submit a batch job on DKRZ HPC?")

    assert result.result == ""
    assert f"HTTP {status}" in result.error
    assert "API key" in result.error
    # No config details, upstream noise or key fragments reach the user.
    assert "CLIMATECLAW_" not in result.error
    assert "sk-" not in result.error
    assert "OpenAIException" not in result.error
    # Operators find the config hint in the server log instead.
    assert "CLIMATECLAW_OPENAI_API_KEY" in caplog.text
    assert "CLIMATECLAW_LITELLM_MASTER_KEY" in caplog.text


async def test_other_http_errors_still_raise(failing_completion):
    failing_completion(500, "LiteLLM returned HTTP 500: boom")

    with pytest.raises(RuntimeError, match="Web-search failed: .*HTTP 500"):
        await ws.web_search("query")
