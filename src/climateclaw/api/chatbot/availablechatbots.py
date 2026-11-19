import httpx
from fastapi import APIRouter, HTTPException

from climateclaw.core.available_chatbots import available_chatbots
from climateclaw.services.service_factory import AuthRequired

router = APIRouter()


@router.get("/availablechatbots", response_model=list[str], dependencies=[AuthRequired])
async def available_chatbots_endpoint() -> list[str]:
    """
    Retrieve Available Chatbots.

    Returns the ordered list of chatbot models the LiteLLM proxy serves, as
    listed by its /v1/models. Models containing the substring "embed" are
    excluded from the result.
    Requires a valid authenticated user.

    Returns:
        List[str]: A list of chatbot model identifiers, in the proxy's order.
        The first item in the list is considered the default model by the
        frontend if no explicit model is selected.

    Raises:
        HTTPException (502):
            - If LiteLLM can't be reached or rejects the request.
            - If LiteLLM lists no models.
    """
    try:
        chatbot_list = await available_chatbots()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not get the available chatbots from LiteLLM: {e}",
        ) from e

    return [c for c in chatbot_list if "embed" not in c]
