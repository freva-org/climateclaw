"""The backend gets its model list from the LiteLLM proxy.

Needs a running proxy and skips without one: the dev stack's (``./dev.sh up litellm``)
by default, or the one CLIMATECLAW_TEST_LITELLM_URL names, e.g. https://llm.dkrz.de
with that proxy's key in CLIMATECLAW_LITELLM_API_KEY.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest

from climateclaw.core import available_chatbots
from climateclaw.services.streaming import litellm_client

pytestmark = pytest.mark.integration

LITELLM_URL = os.getenv("CLIMATECLAW_TEST_LITELLM_URL", "http://localhost:4000")


def _litellm_is_up() -> bool:
    try:
        httpx.get(f"{LITELLM_URL}/v1/models", timeout=5)
    except httpx.HTTPError:
        return False
    return True


needs_litellm = pytest.mark.skipif(
    not _litellm_is_up(), reason=f"no LiteLLM at {LITELLM_URL}"
)


@pytest.fixture
def litellm_at_test_url(monkeypatch):
    """Send the client's requests to the proxy under test."""
    monkeypatch.setattr(
        litellm_client, "_api_url", lambda endpoint: f"{LITELLM_URL}/{endpoint}"
    )
    available_chatbots.refresh_cache()
    yield
    available_chatbots.refresh_cache()


@needs_litellm
@pytest.mark.asyncio
async def test_list_models_returns_the_proxy_models(litellm_at_test_url):
    models = await litellm_client.list_models()

    assert models
    assert all(isinstance(m, str) and m for m in models)


@needs_litellm
@pytest.mark.skipif(
    urlparse(LITELLM_URL).hostname not in {"localhost", "127.0.0.1", "::1"},
    reason=f"LiteLLM at {LITELLM_URL} is not the local one",
)
@pytest.mark.asyncio
async def test_backend_chatbots_are_the_proxy_models(litellm_at_test_url):
    """The backend offers exactly what the proxy lists, with the first as default."""
    proxy_models = await litellm_client.list_models()

    assert await available_chatbots.available_chatbots() == proxy_models
    assert await available_chatbots.default_chatbot() == proxy_models[0]
