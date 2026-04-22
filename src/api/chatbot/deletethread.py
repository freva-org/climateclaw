from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends

from src.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/deletethread", dependencies=[AuthRequired])
async def delete_thread(
    thread_id: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Delete a Chat Thread.

    Deletes a conversation thread from storage.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str):
            The unique identifier of the thread to delete. Must be provided
            as a query parameter.

    Dependencies:
        auth (Authenticator): Injected authentication object containing
            username

    Returns:
        dict:
            A confirmation message if the thread was deleted.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
        HTTPException (500):
            - If deletion fails due to an internal storage error.
    """
    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    Storage = await get_thread_storage()

    try:
        thread_owner = await Storage.get_user_id_for_thread(thread_id)
        # Only allow the deletion of the thread if the user is the owner of the thread
        if thread_owner and thread_owner != auth.username:
            raise HTTPException(
                status_code=403,
                detail="You are not the owner of this thread.",
            )

        await Storage.delete_thread(thread_id)
        logger.info(
            "Deleted thread from storage",
            extra={"thread_id": thread_id, "user_id": auth.username},
        )
        return {"detail": "Thread deleted."}
    except Exception as e:
        logger.exception(
            "Failed to delete thread from storage",
            extra={"thread_id": thread_id, "user_id": auth.username, "error": str(e)},
        )
        raise HTTPException(
            status_code=500, detail="Failed to remove thread from storage."
        )
