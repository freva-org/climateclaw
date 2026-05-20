from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)
from freva_gpt.services.storage.mongodb_storage import ThreadStorage

router = APIRouter()


@router.get("/searchthreads", dependencies=[AuthRequired])
async def search_threads(
    query: str,
    page: int = 0,
    num_threads: int = 20,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Search User Threads.

    Searches the authenticated user's conversation threads using a query
    string. Supports only topic-based search.
    Requires a valid authenticated user.

    Parameters:
        query (str):
            The search query string.
        num_threads (int):
            The maximum number of matching threads to return. Optional, defaults to 20.
        page (int):
            The page number for pagination (reserved for paging logic). Optional, starts at 0.

    Dependencies:
        auth (Authenticator): Injected authentication object containing
            username

    Returns:
        List[Any]:
            A two-element list containing:
                1. A list of matching thread metadata dictionaries, each including:
                   - user_id (str)
                   - thread_id (str)
                   - date (datetime | str)
                   - topic (str)
                   - content (Any)
                2. The total number of matching threads (int).

    Raises:
        HTTPException (422):
            - If the authenticated user ID is missing.
            - If the query parameter is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If querying threads fails due to an internal error.
    """
    logger = configure_logging(__name__, user_id=auth.username)

    if not auth.username:
        raise HTTPException(
            status_code=422,
            detail="Missing user_id (auth).",
        )

    if not query:
        raise HTTPException(
            status_code=422,
            detail="Missing query parameter.",
        )

    num_threads = num_threads or 20  # default to 20 if not provided
    page = page or 0  # default to 0 if not provided

    try:
        total_num_threads, threads = await storage.query_by_topic(
            auth.username, query, num_threads, page
        )
    except Exception as e:
        logger.exception("Failed to query threads: %s", e)
        raise HTTPException(status_code=500, detail="Failed to query threads.")

    return [
        [
            {
                "user_id": t.user_id,
                "thread_id": t.thread_id,
                "date": t.date,
                "topic": t.topic,
                "content": t.content,
            }
            for t in threads
        ],
        total_num_threads,
    ]
