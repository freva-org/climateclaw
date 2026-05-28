from typing import List
from pathlib import Path
from dataclasses import dataclass

import os
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from src.core.settings import get_settings, Settings
from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import StreamVariant

DEFAULT_LOGGER = configure_logging(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings: Settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGODB_COLLECTION_NAME

CACHE_ROOT = Path("./cache")


# ──────────────────────────── Model ───────────────────────────────────
@dataclass
class Thread:
    user_id: str
    thread_id: str
    date: str  # ISO 8601
    topic: str
    content: List[StreamVariant]


# ──────────────────── Helper Functions ──────────────────────────────


def create_dir_at_cache(user_id: str, thread_id: str) -> None:
    """
    Create cache/{user_id}/{thread_id}. On failure (e.g., non-alphanumeric user_id),
    retry with a sanitized user_id (keep only [A-Za-z0-9]). Logs but never raises.
    """
    cache = CACHE_ROOT / thread_id
    try:
        cache.mkdir(parents=True, exist_ok=True)
        DEFAULT_LOGGER.debug("cache created or exists: %s", cache)
        return
    except Exception as e:
        DEFAULT_LOGGER.debug(
            "Failed to create cache=%s, err=%s -- retrying with sanitized user_id",
            cache,
            e,
        )


# ──────────────────── Connection ──────────────────────────────


def get_mongodb_uri() -> str:
    user = os.getenv("FREVAGPT_MONGODB_USER", "")
    password = os.getenv("FREVAGPT_MONGODB_PASSWORD", "")
    if not user or not password:
        raise ValueError("Please set the MongoDB user and password in environment variables!")
    uri = f"mongodb://{user}:{password}@mongodb:27017"
    return uri
