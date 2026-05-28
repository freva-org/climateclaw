from fastapi import Request
from src.core.logging_setup import configure_logging
from .authenticator import Authenticator

from src.core.settings import get_settings

log = configure_logging(__name__)


class DevAuthenticator(Authenticator):
    async def build(request: Request) -> Authenticator:
        """
        DEV mode:
        - no real token validation
        - username, rest_url come from headers or defaults
        """

        if request:
            username = request.headers.get("x-dev-user", "janedoe")
            rest_url = request.headers.get("x-dev-rest-url", "http://dev-rest")
            access_token = request.headers.get("Authorization", "Access-token")
        else:
            username = "janedoe"
            rest_url = "http://dev-rest"
            access_token = "Access-token"

        log.info(
            "DEV auth applied",
            extra={"user_id": username, "rest_url": rest_url},
        )

        return DevAuthenticator(
            request=request,
            settings=get_settings(),
            username=username,
            rest_url=rest_url,
            access_token=access_token,
        )
