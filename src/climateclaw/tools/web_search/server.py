import asyncio
import os

from fastmcp import FastMCP
from openai import AsyncOpenAI

from climateclaw.core.logging_setup import configure_logging
from climateclaw.tools.active_requests import (
    ACTIVE_REQUESTS,
    RequestCancelled,
    current_ids,
    tracked_request,
)
from climateclaw.tools.header_gate import make_header_gate

SERVICE_NAME = os.getenv("HOSTNAME") or "web_search_server"

logger = configure_logging(__name__, named_log=SERVICE_NAME)


OPENAI_API_KEY = os.getenv("CLIMATECLAW_OPENAI_API_KEY", "")

mcp = FastMCP("web-search-server")

# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL = "gpt-4.1"
ALLOWED_DOMAINS = [
    "docs.dkrz.de",
    "docs.icon-model.org",
    "easy.gems.dkrz.de",
]

HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8052"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path

# ─── App ────────────────────────────────────────────────────────────────────

logger.info("Starting Web-Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[],
    header_name_list=[],
    logger=logger,
    mcp_path=PATH,
    on_cancel_request=ACTIVE_REQUESTS.cancel,
)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ─── Tool ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def web_search(query: str) -> dict:
    """
    Calls a web-search agent to access DKRZ/HPC and ICON model documentation website.
    Args:
        query (str): The user's (or LLMs) query.
    Returns:
        str: Relevant context extracted from web-page.
    """
    sid, rid = current_ids()

    logger.info(
        "Searching for DKRZ/HPC- or ICON-related context in documentation "
        f"for query: {query}"
    )

    try:
        async with tracked_request(sid, rid) as req:
            req.raise_if_cancelled()

            system_prompt = (
                "You are a web-search agent that can search documentations for ICON model, EASYGEMS "
                "and DKRZ/HPC. Use the documentation websites for searching and creating "
                "answers. Make sure the information provided is accurate and up-to-date. "
                "DKRZ/HPC doc 'https://docs.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2'. "
                "ICON doc 'https://docs.icon-model.org/search.html?q=SEARCHTERM1+SEARCHTERM2'. "
                "EasyGems doc 'https://easy.gems.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2'."
                "Use SEARCHTERM 1 and 2 to find relevant information. Only answer questions "
                "if claims can be supported by web citations. Include inline citations for "
                "URLs found in the web search results."
            )

            user_content = [{"type": "input_text", "text": query or ""}]

            kwargs = {
                "model": WEB_SEARCH_MODEL,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
                "tool_choice": "auto",
                "tools": [
                    {
                        "type": "web_search",
                        "filters": {"allowed_domains": ALLOWED_DOMAINS},
                    }
                ],
                "include": ["web_search_call.action.sources"],
            }
            logger.info(kwargs)

            call = asyncio.create_task(
                client.responses.create(**kwargs)  # type: ignore[call-overload]
            )
            waiter = asyncio.create_task(req.cancelled_async.wait())

            try:
                done, pending = await asyncio.wait(
                    {call, waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                if waiter in done:
                    raise RequestCancelled("Web-search cancelled by client")

                resp = call.result()

            finally:
                # defensive cleanup
                for task in (call, waiter):
                    if not task.done():
                        task.cancel()

            req.raise_if_cancelled()

            logger.info(f"Successfully completed web search with query {query}.\n")

            return {"result": resp.output_text, "error": ""}

    except asyncio.CancelledError:
        raise

    except RequestCancelled:
        logger.info("Web-search cancelled by client. sid=%s rid=%s", sid, rid)
        return {"result": "", "error": "Request cancelled by client."}

    except Exception as e:
        logger.warning("Web-search failed due to error: %s", e)
        raise RuntimeError(f"Web-search failed: {e}") from e
