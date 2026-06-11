# custom_auth.py
from fastapi import Request
from litellm.proxy._types import UserAPIKeyAuth
import os

BACKEND_SECRET_KEY = os.getenv("FREVAGPT_LITE_LLM_BACKEND_KEY")
VSCODE_SECRET_KEY = os.getenv("FREVAGPT_LITE_LLM_VSCODE_KEY")


def get_rights_from_key(api_key: str) -> tuple[str, list[str]] | None:
    # The backend key is secret and only shared between the backend and Litellm.
    # It gives total access to all models and is used for internal communication.
    # TODO: maybe do proper freva auth?
    # That way, instead of asking for the key, they could insert their own token, having the downside of it timing out within one hour.

    if api_key == BACKEND_SECRET_KEY and BACKEND_SECRET_KEY is not None:
        return ("backend-user", ["*"])  # Grant access to all models
    if api_key == VSCODE_SECRET_KEY and VSCODE_SECRET_KEY is not None:
        return (
            "vscode-user",
            ["gemma4:26b"],
        )  # Grant access to only the local models we want to expose to VSCode
    return None


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    entry = get_rights_from_key(api_key)
    if entry is None:
        raise Exception("Invalid API key")

    return UserAPIKeyAuth(
        api_key=api_key,
        user_id=entry[0],
        models=entry[1],
    )
