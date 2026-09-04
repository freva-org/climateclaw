from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import AuthRequired
from climateclaw.services.streaming.active_conversations import request_stop

router = APIRouter()


class StopRequest(BaseModel):
    thread_id: str | None = None


@router.post("/stop", dependencies=[AuthRequired])
async def stop(
    request: StopRequest,
):
    """
    Stop Active Conversation Streaming.

    Signals that an active conversation associated with the given thread
    should stop streaming and cancels any in-flight tool executions.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str | None):
            The unique identifier of the thread whose streaming process
            should be stopped. Must be provided in the request body.

    Returns:
        dict:
            A confirmation message if the stop signal was successfully
            issued for the specified thread.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
        HTTPException (404):
            - If no active conversation with the given thread ID was found.
        HTTPException (500):
            - Failure to request stop.
    """
    thread_id = request.thread_id

    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Thread ID is missing. Please provide a thread_id in the request body.",
        )

    logger = configure_logging(__name__, thread_id=thread_id)

    try:
        ok = await request_stop(thread_id)
        if ok:
            logger.debug("Initiated stop request", extra={"thread_id": thread_id})
            return {"detail": "Conversation stopped."}
        logger.warning(
            f"Thread not found in the registry. Nothing to stop: {thread_id}"
        )
        raise HTTPException(
            status_code=404,
            detail=f"Conversation with given thread-id not found in the registry: {thread_id}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to stop the thread {thread_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to stop the conversation with thread-id: {thread_id}",
        )
