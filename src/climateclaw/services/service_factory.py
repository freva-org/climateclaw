from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.settings import get_server_url_dict, get_settings

from .authentication.auth import Authenticator
from .mcp.mcp_manager import McpManager, get_mcp_headers
from .storage.mongodb_storage import ThreadStorage

log = configure_logging(__name__)

settings = get_settings()

CACHE_ROOT = Path("./cache")

bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    auto_error=False,
)


async def auth_dependency(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ] = None,
    rest_url: Annotated[
        str | None,
        Header(alias="x-freva-rest-url"),
    ] = None,
) -> Authenticator:
    """
    FastAPI dependency:
    - returns the authenticated object (or raises HTTPException)
    """
    # These parameters let FastAPI describe the request in OpenAPI.
    # Authenticator.build remains the source of truth to preserve existing
    # runtime validation, fallback headers, and error responses.
    _ = credentials, rest_url
    auth = await Authenticator.build(request)
    return auth


# Convenience alias for router-wide protection:
AuthRequired = Depends(auth_dependency)


def get_thread_storage(request: Request) -> ThreadStorage:
    storage = getattr(request.app.state, "thread_storage", None)
    if storage is None:
        raise HTTPException(
            status_code=503,
            detail="Thread storage is not initialized.",
        )
    return storage


async def get_mcp_manager(
    authenticator: Authenticator, thread_id: str
) -> McpManager | None:
    """
    Build and eagerly initialize a manager so tools are ready for prompting.
    """
    # Defaults to send; per-call headers (rest) are added at call time.
    default_headers: dict[str, str] = {
        "X-Freva-Thread-Id": thread_id,
    }

    logger = configure_logging(__name__, thread_id=thread_id)

    try:
        MCP_SERVER_URLs = get_server_url_dict(settings.AVAILABLE_MCP_SERVERS)
    except ValueError as e:
        logger.warning("MCP manager initialization failed: %s", e)
        return None

    mgr = McpManager(
        servers=settings.AVAILABLE_MCP_SERVERS,
        server_urls=MCP_SERVER_URLs,
        default_headers=default_headers,
        logger=logger,
    )

    cache = CACHE_ROOT / thread_id

    extra_headers = get_mcp_headers(authenticator, cache)

    try:
        await mgr.initialize(extra_headers)
        logger.info("Successfully initialized the MCPManager!")
        return mgr
    except Exception as e:
        # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
        logger.warning(
            "MCP manager initialization failed (tools may be unavailable): %s", e
        )
        return None
