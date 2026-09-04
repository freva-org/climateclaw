from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import (
    Authenticator,
    AuthRequired,
    ThreadStorage,
    auth_dependency,
    get_thread_storage,
)

router = APIRouter()


class SetThreadTopicRequest(BaseModel):
    thread_id: str
    topic: str


@router.post("/setthreadtopic", dependencies=[AuthRequired])
async def set_thread_topic(
    request: SetThreadTopicRequest,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Update Thread Topic.

    Updates the topic/title of a specific conversation thread belonging
    to the authenticated user.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str):
            The unique identifier of the thread to update. Must be provided
            as a query parameter.
        topic (str):
            The new topic/title string to assign to the thread.

    Dependencies:
        auth (Authenticator): Injected authentication object containing
            username

    Returns:
        dict:
            A success confirmation message if the thread topic was updated.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If updating the thread topic fails due to an internal error.
    """

    thread_id = request.thread_id
    topic = request.topic

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    try:
        thread_owner = await storage.get_user_id_for_thread(thread_id)
        # Only allow the update of the thread topic if the user is the owner of the thread
        if thread_owner and thread_owner != auth.username:
            raise HTTPException(
                status_code=403,
                detail="You are not the owner of this thread.",
            )
        await storage.update_thread_topic(thread_id, topic)
        logger.info(
            "Updated thread topic",
            extra={"thread_id": thread_id, "user_id": auth.username},
        )
        return {"detail": "Topic updated."}
    except Exception as e:
        logger.exception(
            "Failed to update thread topic",
            extra={"thread_id": thread_id, "user_id": auth.username, "error": str(e)},
        )
        raise HTTPException(status_code=500, detail="Failed to update thread topic.")
