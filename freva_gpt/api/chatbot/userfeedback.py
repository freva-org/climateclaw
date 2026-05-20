from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    ThreadStorage,
    auth_dependency,
    get_thread_storage,
)
from freva_gpt.services.streaming.active_conversations import (
    check_thread_exists,
    save_feedback_to_registry,
)

router = APIRouter()


@router.get("/userfeedback", dependencies=[AuthRequired])
async def user_feedback(
    thread_id: str,
    feedback_index: int,
    feedback: str,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Add or remove user feedback for a specific message within a thread.

    This endpoint allows an authenticated user to attach feedback to a
    specific entry (by index) in an existing conversation thread.
    If `feedback` is set to `"remove"`, the feedback at the given index
    will be deleted instead.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str):
            Unique identifier of the thread containing the content.
            Must correspond to an existing stored thread.

        feedback_index (int):
            Zero-based index of the message within feedback variants (Code
            and Assistant) where feedback should be added or removed.
            Must be within the bounds of the item list in the thread.

        feedback (str):
            The feedback value to store (e.g., "up", "down", text note).
            If set to the literal string `"remove"`, the existing feedback
            at the specified index will be deleted.

    Dependencies:
        auth (Authenticator):
            Injected authentication object containing:
            - username (used as user_id)

    Returns:
        dict:
            - {"detail": "Feedback saved."} on successful save.
            - {"detail": "Feedback removed."} on successful deletion.

    Raises:
        HTTPException (422):
            - Missing thread_id
            - feedback_at_index out of bounds

        HTTPException (404):
            - Thread not found
            - Feedback not found at specified index (on removal)

        HTTPException (500):
            - Error reading thread file
            - Failure while saving or deleting feedback

        HTTPException (503):
            - Failure connecting to thread storage (MongoDB)
    """

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    # Load the thread content
    try:
        content_json = await storage.read_thread(thread_id=thread_id)
    except FileNotFoundError:
        logger.exception(f"Thread not found: {thread_id}")
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        logger.exception(f"Error reading thread file: {thread_id}")
        raise HTTPException(status_code=500, detail="Error reading thread file.")

    feedback_variants = ["Assistant", "Code"]

    # Count the number of feedback messages (Code and Assistant) and check index within bounds
    feedback_message_count = sum(
        1 for msg in content_json if msg.get("variant") in feedback_variants
    )
    if feedback_index < 0 or feedback_index >= feedback_message_count:
        raise HTTPException(
            status_code=422,
            detail="feedback_index outside feedback variant range! Please review query parameters!",
        )

    # Find the position of the Nth feedback message
    feedback_msg_seen = 0
    feedback_at_thread_index = None
    for i, msg in enumerate(content_json):
        if msg.get("variant") in feedback_variants:
            if feedback_msg_seen == feedback_index:
                feedback_at_thread_index = i
                break
            feedback_msg_seen += 1
    if feedback_at_thread_index is None:
        raise HTTPException(
            status_code=422,
            detail="Could not find the specified feedback message index! Please review query parameters!",
        )

    if feedback != "remove":
        try:
            await save_feedback(
                storage,
                thread_id,
                auth.username,
                content_json,
                feedback_at_thread_index,
                feedback,
            )
            logger.info(
                f"Successfully saved user feedback at index {feedback_at_thread_index}: {thread_id}"
            )
            return {"detail": "Feedback saved."}
        except Exception:
            logger.exception(
                f"Failed to save user feedback at index {feedback_at_thread_index}: {thread_id}"
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to save user feedback: {thread_id}"
            )
    else:
        # TODO: delete feedback when user deletes thread?
        if "feedback" not in content_json[feedback_at_thread_index].keys():
            logger.exception(
                f"Feedback not found at thread index {feedback_at_thread_index}: {thread_id}"
            )
            raise HTTPException(
                status_code=404,
                detail=f"Feedback not found at thread index {feedback_at_thread_index}: {thread_id}",
            )
        try:
            await delete_feedback(
                storage,
                thread_id,
                auth.username,
                content_json,
                feedback_at_thread_index,
            )
            logger.info(
                f"Successfully removed user feedback at index {feedback_at_thread_index}: {thread_id}"
            )
            return {"detail": "Feedback removed."}
        except Exception:
            logger.exception(f"Failed to delete user feedback: {thread_id}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete user feedback at index {feedback_at_thread_index}: {thread_id}",
            )


async def save_feedback(
    storage: ThreadStorage, thread_id, user_id, content, f_ind, feedback
):
    await storage.save_feedback(thread_id, user_id, content, f_ind, feedback)
    if await check_thread_exists(thread_id):
        await save_feedback_to_registry(thread_id, f_ind, feedback)


async def delete_feedback(storage: ThreadStorage, thread_id, user_id, content, f_ind):
    await storage.delete_feedback(thread_id, user_id, content, f_ind)
    if await check_thread_exists(thread_id):
        await save_feedback_to_registry(thread_id, f_ind, "remove")
