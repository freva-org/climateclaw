from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import HTTPException
from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.settings import Settings, get_settings
from freva_gpt.services.streaming.stream_variants import StreamVariant
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

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
    content: list[StreamVariant]


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


async def get_mongodb_uri(vault_url: str) -> str:
    # 1) GET vault_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(vault_url)
    except Exception:
        # 503 ServiceUnavailable
        raise HTTPException(status_code=503, detail="Error sending request to vault.")
    if not r.is_success:
        # 502 BadGateway
        raise HTTPException(
            status_code=502,
            detail="Failed to get MongoDB URL. Is Nginx running correctly?",
        )

    # 2) Parse JSON and extract key
    try:
        data = r.json()
    except Exception:
        # 502 BadGateway
        raise HTTPException(status_code=502, detail="Vault response was malformed.")

    uri = data.get("mongodb.url") or data.get("mongo.url")
    if not uri:
        # 502 BadGateway
        raise HTTPException(
            status_code=502, detail="MongoDB URL not found in vault response."
        )
    return uri.strip()


async def get_database(vault_url: str) -> AsyncDatabase:
    """
    Parity with Rust: fetch URI from vault via auth.get_mongodb_uri, connect with Motor.
    If connection fails, retry once without URI options (strip trailing ?query).
    """
    mongodb_uri = await get_mongodb_uri(vault_url)

    client: AsyncMongoClient = AsyncMongoClient(mongodb_uri, connectTimeoutMS=30000)
    return client[MONGODB_DATABASE_NAME]
