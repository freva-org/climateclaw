import os
from typing import Optional

from fastmcp import FastMCP
from openai import OpenAI

from climateclaw.core.logging_setup import configure_logging
from climateclaw.tools.header_gate import make_header_gate

logger = configure_logging(__name__, named_log="web_search_server")

OPENAI_API_KEY: Optional[str] = os.getenv("CLIMATECLAW_OPENAI_API_KEY")

HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8052"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path

# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL = "gpt-5.4-mini"
ALLOWED_DOMAINS = [
    "docs.dkrz.de",
    "docs.icon-model.org",
]

# ─── App ────────────────────────────────────────────────────────────────────
# Per-request header context
mcp = FastMCP("web-search-server")
logger.info("Starting Web-Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[],
    header_name_list=[],
    logger=logger,
    mcp_path=PATH,
)

client = OpenAI(api_key=OPENAI_API_KEY)


@mcp.tool()
def web_search(query: str) -> str:
    """
    Calls a web-search agent to access DKRZ/HPC and ICON model documentation websites.
    Use this for DKRZ infrastructure, Slurm, and ICON model documentation questions ONLY.
    Do NOT use this for Freva plugin source code — use plugin_code_search instead.
    Args:
        query (str): The user's (or LLMs) query.
    Returns:
        str: Relevant context extracted from web-page.
    """
    logger.info(
        "Searching for DKRZ/HPC- or ICON-related context in documentation "
        f"for query: {query}"
    )
    system_prompt = (
        "You are a web-search agent that can search documentations for ICON model "
        "and DKRZ/HPC. Use the documentation websites for searching and creating "
        "answers. Make sure the information provided is accurate and up-to-date. "
        "DKRZ/HPC doc 'https://docs.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2'. "
        "ICON doc 'https://docs.icon-model.org/search.html?q=SEARCHTERM1+SEARCHTERM2'. "
        "Use SEARCHTERM 1 and 2 to find relevant information. Only answer questions "
        "if claims can be supported by web citations. Include inline citations for "
        "URLs found in the web search results.\n\n"
    )
    user_query = query or ""

    kwargs = {
        "model": WEB_SEARCH_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "stream": False,
        "tool_choice": "auto",
        "tools": [
            {"type": "web_search", "filters": {"allowed_domains": ALLOWED_DOMAINS}}
        ],
        "include": ["web_search_call.action.sources"],
    }

    try:
        resp = client.responses.create(**kwargs)

        logger.info(f"Succesfully completed web search with query {query}.\n")
        return resp.output_text
    except Exception as e:
        logger.warning("Web-search failed due to error: %s", e)
        return "(web search failed)"


def debug():
    question = "How do I submit a job to the DKRZ HPC?"
    resp = web_search(question)
    print(resp)
