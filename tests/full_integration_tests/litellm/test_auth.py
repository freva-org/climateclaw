"""The key the client sends must authenticate with the LiteLLM proxy.

Needs a running proxy and skips without one: the dev stack's (``./dev.sh up litellm``)
by default, or the one CLIMATECLAW_TEST_LITELLM_URL names. The tests only list
models, so LiteLLM checks the key without calling any provider.
"""

from __future__ import annotations

import os

import httpx
import pytest

from climateclaw.services.streaming.litellm_client import _headers

pytestmark = pytest.mark.integration

LITELLM_URL = os.getenv("CLIMATECLAW_TEST_LITELLM_URL", "http://localhost:4000")


def _list_models(headers: dict[str, str]) -> httpx.Response:
    return httpx.get(f"{LITELLM_URL}/v1/models", headers=headers, timeout=10)


def _litellm_is_up() -> bool:
    try:
        _list_models({})
    except httpx.HTTPError:
        return False
    return True


needs_litellm = pytest.mark.skipif(
    not _litellm_is_up(), reason=f"no LiteLLM at {LITELLM_URL}"
)


@needs_litellm
def test_litellm_accepts_the_client_key():
    assert _list_models(_headers()).status_code == 200


@needs_litellm
def test_litellm_rejects_a_wrong_key():
    """Without this, the test above would also pass on a proxy with auth off."""
    if _list_models({}).is_success:
        pytest.skip("LiteLLM runs without auth")

    assert not _list_models({"Authorization": "Bearer sk-not-the-key"}).is_success
