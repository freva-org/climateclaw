import httpx
from climateclaw.core.logging_setup import configure_logging
from fastapi import HTTPException, status


def bearer_token_from_header(header_val: str) -> str:
    # The header can be any value, we only allow String.
    if not isinstance(header_val, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Authorization header is not a valid UTF-8 string.",
        )
    # The Authentication header is a Bearer token, so we need to extract the token from it.
    if not header_val.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Authorization header is not a Bearer token. Please use the Bearer token format.",
        )
    return header_val[len("Bearer ") :]


def _normalize_systemuser_path(rest_url: str) -> str:
    """
    The entire url ending is "/api/freva-nextgen/auth/v2/systemuser",
    But it sometimes doesn't send the api and nextgen part, so we need to add it ourselves.
    """
    if rest_url.endswith("/api/freva-nextgen/auth/v2/systemuser"):
        return ""
    if rest_url.endswith("/api/freva-nextgen/"):
        return "auth/v2/systemuser"
    if rest_url.endswith("/api/freva-nextgen"):
        return "/auth/v2/systemuser"
    return "/api/freva-nextgen/auth/v2/systemuser"


async def get_username_from_token(token: str, rest_url: str, logger=None) -> str:
    """
    Calls the token-validation endpoint at <rest_url>/api/freva-nextgen/auth/v2/systemuser
    and returns the username (pw_name).
    """
    log = logger or configure_logging(__name__)

    path = _normalize_systemuser_path(rest_url)
    url = f"{rest_url}{path}"
    log.debug("Token check URL: %s", url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        # ServiceUnavailable on request error to rest
        log.error("Error sending request to systemuser endpoint: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error sending token validation request, is the URL correct?",
        )

    # parse JSON and extract username
    text = resp.text
    log.debug(
        "Token validation success status=%s body=%s", resp.status_code, text[:500]
    )
    try:
        data = resp.json()
    except Exception as e:
        log.error("Error parsing token validation response: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Token validation response is malformed, not valid JSON.",
        )

    # systemuser returns 403 for invalid token and guest
    if not (200 <= resp.status_code < 300):
        # check detail
        detail = data.get("detail")
        if detail == "Token expired.":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed, token may be expired or invalid.",
            )
        elif detail == "Not a system user.":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token validation failed, guest users don't have access to this service."
                "Please make sure to login using a DKRZ account and try again!",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed.",
            )

    username = data.get("username")
    if isinstance(username, str) and username:
        return username

    # 502 when JSON has no username
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Token validation response is malformed, no username found.",
    )
