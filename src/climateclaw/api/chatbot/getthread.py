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
from climateclaw.services.streaming.stream_orchestrator import get_conversation_history
from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVDict,
    SVStreamEnd,
    from_sv_to_json,
    is_prompt,
)

router = APIRouter()


class GetThreadRequest(BaseModel):
    thread_id: str | None = None


def _post_process(variants: list[StreamVariant]) -> list[SVDict]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in variants if not is_prompt(item)]
    cleaned: list[SVDict] = []
    for i, v in enumerate(items):
        if isinstance(v, SVStreamEnd):
            is_last = i == len(items) - 1
            if (not is_last) or ("unexpected manner" in (v.content or "").lower()):
                continue
        cleaned.append(from_sv_to_json(v))
    return cleaned


@router.post("/getthread", dependencies=[AuthRequired])
async def get_thread(
    request: GetThreadRequest,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Retrieve a Chat Thread.

    Returns the full conversation content of a specific thread as a list
    of JSON objects.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str | None):
            The unique identifier of the thread to retrieve. Must be provided
            as a query parameter.

    Dependencies:
        auth (Authenticator): Injected authentication object containing
            username

    Returns:
        List[dict]:
            A list of conversation message objects representing the thread
            history after post-processing.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (404):
            - If the requested thread does not exist.
        HTTPException (500):
            - If an error occurs while reading or processing the thread.
    """

    thread_id = request.thread_id

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    try:
        messages = await get_conversation_history(
            thread_id=thread_id,
            Storage=storage,
        )
        # If the messages are None, it means there was no Storage to read from and we raise a 404.
        if not messages:
            raise FileNotFoundError(f"Thread with ID {thread_id} not found.")
    except FileNotFoundError:
        logger.exception("Thread not found.", extra={"thread_id": thread_id})
        raise HTTPException(status_code=404, detail="Thread not found.")
    except ValueError as e:
        logger.exception(
            f"Error reading thread file: {e}", extra={"thread_id": thread_id}
        )
        raise HTTPException(status_code=500, detail=f"Error reading thread file: {e}")

    content: list[SVDict] = _post_process(messages)

    logger.info(
        "Fetched thread content.",
        extra={"thread_id": thread_id, "user_id": auth.username},
    )

    return content
