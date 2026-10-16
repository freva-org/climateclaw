"""One pooled HTTP client shared by all outgoing calls of the backend.

Creating an ``httpx.AsyncClient`` is expensive (it builds an SSL context and
loads the CA bundle) and throws away open connections. Use
:func:`get_http_client` instead and pass a per-request ``timeout``; never
close the returned client yourself, the app does that on shutdown.
"""

from __future__ import annotations

import asyncio
import weakref

import httpx

# Callers pass their own timeouts; this is only the fallback.
DEFAULT_TIMEOUT = httpx.Timeout(60.0)

# Many long-lived streams (LiteLLM, MCP SSE) can be open at once, so do not
# cap concurrent connections; just bound how many idle ones are kept.
LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=100)

# httpx clients must not be shared across event loops (pytest-asyncio uses a
# new loop per test), so keep one client per loop. In the server that is one
# client per worker process.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def get_http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=LIMITS)
        _clients[loop] = client
    return client


async def aclose_http_client() -> None:
    """Close the client of the running event loop (app shutdown)."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
