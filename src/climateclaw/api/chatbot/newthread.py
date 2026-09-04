from fastapi import APIRouter

from climateclaw.services.service_factory import AuthRequired
from climateclaw.services.streaming.active_conversations import new_thread_id

router = APIRouter()


@router.get("/newthread", response_model=str, dependencies=[AuthRequired])
async def generate_new_thread_id() -> str:
    """
    Request new thread-id.

    Requires a valid authenticated user.

    Returns:
        str: ID for new thread.

    Raises:

    """
    new_id = await new_thread_id()
    return new_id
