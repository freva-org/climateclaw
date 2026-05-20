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


@router.get("/getuserthreads", dependencies=[AuthRequired])
async def get_user_threads(
    num_threads: int = 20,
    page: int = 0,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Retrieve Recent User Threads.

    Returns the most recent conversation threads of the authenticated user,
    limited by the requested number.
    Requires a valid authenticated user.

    Parameters:
        num_threads (int):
            The maximum number of recent threads to return.
        page (int):
            The page number of results to return.

    Dependencies:
        auth (Authenticator): Injected authentication object containing
            username

    Returns:
        List[Any]:
            A two-element list containing:
                1. A list of thread metadata dictionaries, each including:
                   - user_id (str)
                   - thread_id (str)
                   - date (datetime | str)
                   - topic (str)
                   - content (Any)
                2. The total number of threads available for the user
                   (int), independent of the requested limit.

    Raises:
        HTTPException (422):
            - If the authenticated user ID is missing.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If fetching the user's thread history fails.
    """
    logger = configure_logging(__name__, user_id=auth.username)

    if not auth.username:
        raise HTTPException(
            status_code=422,
            detail="Missing user_id (auth).",
        )

    try:
        threads, total_num_threads = await storage.list_recent_threads(
            auth.username, limit=num_threads, page=page
        )

        logger.info(
            "Fetched recent threads",
            extra={
                "user_id": auth.username,
                "thread_count": len(threads),
                "requested": num_threads,
            },
        )

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
    except Exception as e:
        logger.exception(
            "Failed to fetch user history from storage", extra={"error": str(e)}
        )
        raise HTTPException(
            status_code=500, detail="Failed to fetch user history from storage."
        )
