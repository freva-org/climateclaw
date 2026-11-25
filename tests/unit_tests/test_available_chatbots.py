import os
import subprocess
import sys

import pytest

from climateclaw.core import available_chatbots as chatbots


@pytest.fixture
def proxy(monkeypatch):
    """Fake LiteLLM model list; counts how often it is asked."""

    class Proxy:
        models = ["model-a", "model-b"]
        calls = 0

        async def list_models(self) -> list[str]:
            self.calls += 1
            return list(self.models)

    p = Proxy()
    monkeypatch.setattr(chatbots, "list_models", p.list_models)
    chatbots.refresh_cache()
    yield p
    chatbots.refresh_cache()


@pytest.mark.asyncio
async def test_available_chatbots_keeps_the_proxy_order(proxy):
    assert await chatbots.available_chatbots() == ["model-a", "model-b"]
    assert await chatbots.default_chatbot() == "model-a"


@pytest.mark.asyncio
async def test_default_chatbot_picks_the_first_model_matching_the_keyword(proxy):
    proxy.models = ["gpt-4.1", "deepseek-v4.1", "DeepSeek-v5"]
    assert await chatbots.default_chatbot(keyword="deepseek") == "deepseek-v4.1"
    assert await chatbots.default_chatbot(keyword="SEEK-V5") == "DeepSeek-v5"


@pytest.mark.asyncio
async def test_default_chatbot_falls_back_to_the_first_model_without_a_match(proxy):
    assert await chatbots.default_chatbot(keyword="llama") == "model-a"
    assert await chatbots.default_chatbot(keyword="") == "model-a"


@pytest.mark.asyncio
async def test_default_chatbot_warns_when_no_model_matches_the_keyword(proxy, caplog):
    with caplog.at_level("WARNING", logger=chatbots.__name__):
        await chatbots.default_chatbot(keyword="-a")
        assert not caplog.records

        await chatbots.default_chatbot(keyword="llama")
    assert len(caplog.records) == 1
    assert "'llama'" in caplog.text
    assert "model-a" in caplog.text


@pytest.mark.asyncio
async def test_default_chatbot_uses_the_configured_keyword(proxy, monkeypatch):
    monkeypatch.setattr(chatbots, "DEFAULT_MODEL", "-b")
    assert await chatbots.default_chatbot() == "model-b"
    assert await chatbots.default_chatbot(keyword="-a") == "model-a"
    assert await chatbots.default_chatbot(keyword="") == "model-a"


@pytest.mark.asyncio
async def test_available_chatbots_asks_the_proxy_once_per_ttl(proxy, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(chatbots.time, "monotonic", lambda: now)

    await chatbots.available_chatbots()
    await chatbots.available_chatbots()
    assert proxy.calls == 1

    proxy.models = ["model-c"]
    now += chatbots.CACHE_TTL_SECONDS
    assert await chatbots.available_chatbots() == ["model-c"]
    assert proxy.calls == 2


@pytest.mark.asyncio
async def test_available_chatbots_raises_and_does_not_cache_an_empty_list(proxy):
    proxy.models = []
    with pytest.raises(ValueError):
        await chatbots.available_chatbots()

    proxy.models = ["model-a"]
    assert await chatbots.available_chatbots() == ["model-a"]


@pytest.mark.asyncio
async def test_available_chatbots_picks_up_proxy_changes_when_the_cache_expires(
    proxy, monkeypatch
):
    now = 1000.0
    monkeypatch.setattr(chatbots.time, "monotonic", lambda: now)
    assert await chatbots.available_chatbots() == ["model-a", "model-b"]

    proxy.models = ["model-c", "model-a"]

    now += chatbots.CACHE_TTL_SECONDS - 1
    assert await chatbots.available_chatbots() == ["model-a", "model-b"]
    assert await chatbots.default_chatbot() == "model-a"

    now += 1
    assert await chatbots.available_chatbots() == ["model-c", "model-a"]
    assert await chatbots.default_chatbot() == "model-c"


@pytest.mark.asyncio
async def test_refresh_cache_picks_up_proxy_changes_at_once(proxy):
    assert await chatbots.available_chatbots() == ["model-a", "model-b"]

    proxy.models = ["model-c"]
    chatbots.refresh_cache()

    assert await chatbots.available_chatbots() == ["model-c"]


@pytest.mark.parametrize("seconds", ["45", "0"])
def test_cache_time_comes_from_the_environment(seconds):
    """Settings read the environment at import, so check it in a fresh interpreter."""
    env = {
        **os.environ,
        "CLIMATECLAW_MODEL_LIST_CACHE_SECONDS": seconds,
        # The package isn't installed (uv package = false); pytest finds it via its
        # pythonpath setting, which the child doesn't inherit.
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from climateclaw.core import available_chatbots as c; "
            "print(c.CACHE_TTL_SECONDS)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert float(out.strip().splitlines()[-1]) == float(seconds)


def test_default_model_keyword_comes_from_the_environment():
    """Settings read the environment at import, so check it in a fresh interpreter."""
    env = {
        **os.environ,
        "CLIMATECLAW_DEFAULT_MODEL": "deepseek",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from climateclaw.core import available_chatbots as c; "
            "print(c.DEFAULT_MODEL)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert out.strip().splitlines()[-1] == "deepseek"
