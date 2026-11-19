# climateclaw/core/available_chatbots.py
from __future__ import annotations

import logging
import os
import time

from climateclaw.services.streaming.litellm_client import list_models

"""
Model catalog: the models the LiteLLM proxy serves, as listed by its /v1/models.

Behavior:
- Keep the proxy's order: the first model is the default
- Cache the list for CACHE_TTL_SECONDS, so requests don't each query the proxy
- Raise ValueError if the proxy lists no models

- model_is_reasoning: names starting with 'o3', 'o4', or 'gpt-5'
- model_is_gpt_5:    names starting with 'gpt-5'
- model_supports_images: names starting with 'gpt-4o', 'gpt-5', or 'gpt-4.1'
- model_ends_on_no_choice: names starting with 'qwen2_5'
"""

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300.0

# Read once at import time; override via the CLIMATECLAW_DEFAULT_LOCAL_MODEL env variable.
DEFAULT_LOCAL_MODEL = os.getenv("CLIMATECLAW_DEFAULT_LOCAL_MODEL") or "deepseek-v4.1"

# (time fetched, models)
_cache: tuple[float, list[str]] | None = None


async def available_chatbots() -> list[str]:
    """
    Returns the ordered list of models the LiteLLM proxy serves.
    Raises httpx.HTTPError if the proxy can't be reached, ValueError if it lists none.
    """
    global _cache
    if _cache and time.monotonic() - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    models = await list_models()
    if not models:
        logger.error("LiteLLM lists no models.")
        raise ValueError("LiteLLM lists no models.")

    if _cache is None or _cache[1] != models:
        logger.info("Available chatbots (%d): %s", len(models), models)
    _cache = (time.monotonic(), models)
    return models


async def default_chatbot() -> str:
    """
    Default chatbot is the first entry.
    """
    models = await available_chatbots()
    return models[0]


def default_chatbot_local() -> str:
    """
    Default local chatbot.

    Set from ``CLIMATECLAW_DEFAULT_LOCAL_MODEL`` at import time (see ``DEFAULT_LOCAL_MODEL``).
    """
    return DEFAULT_LOCAL_MODEL


# ──────────────────────────────────────────────────────────────────────────────
# Helper predicates
# ──────────────────────────────────────────────────────────────────────────────


def model_is_reasoning(model: str) -> bool:
    """
    True for names starting with 'o3', 'o4', or 'gpt-5'.
    """
    return model.startswith(("o3", "o4", "gpt-5"))


def model_is_gpt_5(model: str) -> bool:
    """
    True for names starting with 'gpt-5'.
    """
    return model.startswith("gpt-5")


def model_is_local(model: str) -> bool:
    """
    True for names starting with the defined list.
    """
    local_models = (
        "mistral",
        "ministral",
        "qwen",
        "llama",
        "deepseek",
        "gemma",
    )
    return model.startswith(local_models)


def model_supports_images(model: str) -> bool:
    """
    True for names starting with the defined list.
    """
    vision_models = ("gpt", "qwen3.6", "gemma4", "deepseek", "mistral", "ministral")
    return model.startswith(vision_models)


def refresh_cache() -> None:
    """
    Forget the cached model list, so the next call asks the proxy again.
    """
    global _cache
    _cache = None


__all__ = [
    "available_chatbots",
    "default_chatbot",
    "model_is_gpt_5",
    "model_is_local",
    "model_is_reasoning",
    "model_supports_images",
    "refresh_cache",
]
