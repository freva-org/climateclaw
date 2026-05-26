from starlette.datastructures import Headers
from fastapi import HTTPException, status, Request
import httpx
from dataclasses import dataclass

from src.core.logging_setup import configure_logging
from src.core.settings import get_settings, Settings

from .helpers import bearer_token_from_header, get_username_from_token

log = configure_logging(__name__)


@dataclass
class Authenticator():
    """
    Checks the Authorization header (Bearer token) or x-freva-user-token + x-f
    The user must send an Authorization header. No fallback logic to the
    previous auth system.
    Returns:
      - self (Authenticator instance)
    Errors:
      - 422/400/401/502/503
    """
    request: Request
    settings: Settings
    username: str
    rest_url: str
    access_token: str

    @classmethod
    async def build(cls, request: Request) -> "Authenticator":
        settings = get_settings()

        headers: Headers = request.headers

        # Checking Authorization header OR x-freva-user-token
        header_val: str | None = headers.get("Authorization") or headers.get(
            "x-freva-user-token"
        )

        if header_val:
            # -> Bearer flow
            try:
                token: str = bearer_token_from_header(header_val)
                access_token: str = token
            except HTTPException as e:
                # Raise exception for non-Bearer
                raise e

            # Checking rest_url
            rest_url: str | None = headers.get("x-freva-rest-url")
            if not rest_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Authentication not successful! RestURL not found. Please use the nginx proxy. (rest)",
                )

            try:
                username: str = await get_username_from_token(
                    token, rest_url, logger=configure_logging(__name__, user_id=None)
                )
                return cls(
                    request=request,
                    settings=settings,
                    username=username,
                    rest_url=rest_url,
                    access_token=access_token,
                )
            except HTTPException as err:
                raise err

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Some necessary field weren't found, check whether the nginx proxy and sets the right headers.",
        )
