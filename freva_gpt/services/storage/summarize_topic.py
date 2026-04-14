from __future__ import annotations

import re
from typing import List

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.streaming.litellm_client import acomplete, first_text
from freva_gpt.services.streaming.stream_variants import StreamVariant, SVUser

DEFAULT_LOGGER = configure_logging(__name__)


_NO_TOPIC = "No topic yet"

_EXACT_LOW_INFO = {
    "hi",
    "hello",
    "hallo",
    "hey",
    "yo",
    "sup",
    "good morning",
    "good afternoon",
    "good evening",
    "quick question",
    "can you help",
    "can you help me",
    "i need help",
    "i need your help",
    "hi there",
}

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str | None) -> str:
    """Normalize whitespace and strip surrounding spaces."""
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _is_low_information(text: str) -> bool:
    """Return True only for obvious exact-match low-information openings."""
    t = _normalize_text(text).lower()
    if not t:
        return True

    return t in _EXACT_LOW_INFO


def _extract_first_meaningful_user_text(content: List[StreamVariant]) -> str:
    """Return the first non-empty user text that is not exact-match filler."""
    for sv in content:
        if isinstance(sv, SVUser):
            text = _normalize_text(getattr(sv, "text", ""))
            if text and not _is_low_information(text):
                return text
    return ""


def _fallback_topic(raw: str | None) -> str:
    if not raw:
        return _NO_TOPIC
    # naive single-line truncation
    s = " ".join(raw.split())
    return (s[:80] + "…") if len(s) > 80 else s


async def summarize_topic(content: List[StreamVariant]) -> str:
    """
    Summarize the chat topic using the first meaningful user message.

    Behavior:
    - Skip the LLM entirely for obvious low-information openings.
    - Use the first meaningful user text as the topic source.
    - On model failure, fall back safely.
    - Never return an empty string.
    """
    thread_id = getattr(content[0], "data", {}).get("thread_id", "") if content else ""

    if thread_id:
        logger = configure_logging(__name__, thread_id=thread_id)
    else:
        logger = DEFAULT_LOGGER

    meaningful_text = _extract_first_meaningful_user_text(content)

    # If the conversation only starts with greetings/filler, do not call the LLM.
    if not meaningful_text:
        logger.info("No meaningful user prompt found, topic set to 'No topic yet'.")
        return _NO_TOPIC

    prompt = (
        "Summarize the user's actual topic in at most 12 words, neutral tone.\n"
        "Ignore greetings, pleasantries, acknowledgements, and filler.\n"
        'If there is no actual topic, return exactly: "No topic yet"\n\n'
        f"Topic:\n{meaningful_text[:2000]}"
    )

    try:
        resp = await acomplete(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4.1-mini",
            max_tokens=30,
            temperature=0.1,
        )
        topic = _normalize_text(first_text(resp))

        if topic:
            logger.info(f"Summarized topic successfully: {topic}")
            return topic

        # Prefer the meaningful text for fallback if available.
        fallback = _fallback_topic(meaningful_text)
        logger.info(f"Empty model response; using fallback: {fallback}")
        return fallback

    except Exception as e:
        logger.warning("Falling back due to error: %s", e)
        fallback = _fallback_topic(meaningful_text)
        return fallback
