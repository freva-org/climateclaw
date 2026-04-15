import json
import os
from typing import Optional
from urllib.parse import quote as urlquote

import httpx
from fastmcp import FastMCP

from src.tools.header_gate import make_header_gate

from src.core.logging_setup import configure_logging
from openai import OpenAI

logger = configure_logging(__name__, named_log="web_search_server")

OPENAI_API_KEY: Optional[str] = os.getenv("FREVAGPT_OPENAI_API_KEY")
GITLAB_TOKEN: Optional[str] = os.getenv("FREVAGPT_GITLAB_TOKEN")

mcp = FastMCP("web-search-server")

# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL = "gpt-4.1"
ALLOWED_DOMAINS = [
    "docs.dkrz.de",
    "docs.icon-model.org",
]

# GitLab plugin config
GITLAB_BASE_URL = "https://gitlab.dkrz.de/api/v4"
PLUGIN_GROUP_PATH = "kd1418/plugins4freva"
FREVA_PLUGINS = [
    "cvprepare",
    "leadtimeselektor",
    "problems",
    "recalibration",
    "terciles"
]
MAX_FILE_SIZE_BYTES = 50_000  # skip files larger than this
MAX_TOTAL_CODE_CHARS = 50_000  # truncate total fetched code after this limit
MAX_RELEVANT_FILES = 5  # max files to fetch after relevance filtering

HOST = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("FREVAGPT_MCP_PORT", "8052"))
PATH = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path

# ─── App ────────────────────────────────────────────────────────────────────

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
        "Use SEARCHTEAM 1 and 2 to find relevant information. Only answer questions "
        "if claims can be supported by web citations. Include inline citations for "
        "URLs found in the web search results.\n\n"
    )
    user_query = query or ""

    kwargs = {
        "model": WEB_SEARCH_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
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


#########################################################################################
# ── GitLab helpers ───────────────────────────────────────────────────────────
_gitlab_http = httpx.Client(
    base_url=GITLAB_BASE_URL,
    headers={"PRIVATE-TOKEN": GITLAB_TOKEN or ""},
    timeout=30.0,
)


def _gitlab_project_id(plugin_name: str) -> str:
    """URL-encoded project path usable as :id in the GitLab v4 API."""
    return urlquote(f"{PLUGIN_GROUP_PATH}/{plugin_name}", safe="")


def _fetch_repo_tree(plugin_name: str) -> list[str]:
    """
    Return the recursive file tree for a plugin repository as a list of
    file paths (strings) that match relevant extensions.
    """
    project_id = _gitlab_project_id(plugin_name)
    items: list[dict] = []
    page = 1
    while True:
        resp = _gitlab_http.get(
            f"/projects/{project_id}/repository/tree",
            params={"recursive": "true", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        page += 1
    
    # filter to file paths with relevant extensions
    paths = [
        entry["path"] for entry in items
        if entry["type"] == "blob" and
        entry["path"].lower().endswith((".py", ".sh", ".md", ".rst"))
        ]
    return paths


def _fetch_plugin_code(plugin_name: str, selected_files: list[str], max_chars: int) -> str:
    """
    Fetch the raw content of selected files and concatenate them into a single string,
    until the total character count reaches `max_chars`.
    The default branch name for all files is "levante".
    """
    def _fetch_file_raw(plugin_name: str, file_path: str, ref: str="levante") -> str:
        project_id = _gitlab_project_id(plugin_name)
        encoded_path = urlquote(file_path, safe="")
        resp = _gitlab_http.get(
            f"/projects/{project_id}/repository/files/{encoded_path}/raw",
            params={"ref": ref},
        )
        resp.raise_for_status()
        return resp.text

    collected: list[str] = []
    total_chars = 0
    for file in selected_files:
        if total_chars >= max_chars:
            collected.append(
                f"\n--- (truncated: reached {max_chars} char limit) ---"
            )
            break
        try:
            content = _fetch_file_raw(plugin_name, file)
            if len(content) > MAX_FILE_SIZE_BYTES:
                content = content[:MAX_FILE_SIZE_BYTES] + "\n... (file truncated)"
            collected.append(f"### FILE: {file}\n```\n{content}\n```\n")
            total_chars += len(content)
        except Exception as e:
            logger.debug("Skipping file %s: %s", file, e)

    if not collected:
        return "(no source files could be retrieved)"

    return "\n".join(collected)


def _search_relevant_files(
    plugin_name: str, query: str, file_paths: list[str], dep: bool=False
) -> list[str]:
    """
    Given a list of file paths in the plugin repo, ask GPT-4.1 to pick the most relevant
    ones for the user's query (dep=False) or for searching code dependencies (dep=True).
    If the LLM-based selection fails, fall back to a heuristic of picking all files.
    """
    if not dep:
        tree_listing = "\n".join(file_paths)
        selection_prompt = (
            f"Below is the file tree of the '{plugin_name}' Freva plugin repository:\n"
            f"File tree:\n{tree_listing}\n\n"
            f"The user's query now is:\n\"{query}\"\n\n"
            "Return ONLY a JSON array of file paths (strings) that seem most relevant to "
            "answering the user's query. Include README / documentation files when helpful. "
            f"Return at most {MAX_RELEVANT_FILES} paths. Output nothing but the JSON array."
        )
    else:
        remain_listing = "\n".join(file_paths)
        selection_prompt = (
            "You are analyzing Python source code from a repository. "
            "The code below contains import statements. Identify which of the REMAINING "
            "repository files are imported or referenced as dependencies by the code.\n\n"
            f"=== FETCHED CODE ===\n{query}\n=== END ===\n\n"
            f"Remaining files in the repository:\n{remain_listing}\n\n"
            "Return ONLY a JSON array of file paths (strings) from the remaining list "
            "that are imported or depend upon the fetched code. "
            "If none are needed, return an empty array []."
        )

    try:
        selection_resp = client.responses.create(
            model=WEB_SEARCH_MODEL,
            input=[{"role": "user", "content": selection_prompt}],
            stream=False,
        )
        raw_text = selection_resp.output_text.strip()
        logger.debug(
            "LLM file selection response for plugin '%s': %s", plugin_name, raw_text
        )
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[1:])
        if raw_text.endswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[:-1])
        selected_files: list[str] = json.loads(raw_text.strip())
    except Exception as e:
        logger.warning(
            "LLM file selection failed (%s); falling back to heuristic.", e
        )
        # Fallback: pick all files from the tree search;
        # or pick nothing for dependency selection to avoid fetching duplicate code
        selected_files = file_paths if not dep else []

    # Ensure we only pick paths that actually exist in the tree
    selected_files = [p for p in selected_files if p in set(file_paths)][:MAX_RELEVANT_FILES]
    stage = "Initial" if not dep else "Dependency"
    logger.info(
        "%s stage selected %d/%d files for plugin '%s': %s",
        stage,
        len(selected_files),
        len(file_paths),
        plugin_name,
        selected_files,
    )
    return selected_files


def _collect_plugin_context(plugin_name: str, user_query: str) -> str:
    """
    Three-stage context retrieval of code base:
        1. Ask GPT-4.1 which files are most relevant for the user's query.
        2. Fetch those files, then scan for imports to identify dependent modules.
        3. Fetch the dependencies and return the combined contents.
    Returns a string containing the concatenated relevant code snippets,
        separated by file and with a header.
    """
    # ── Stage 0: fetch the repository tree with all files ────────────────────
    file_paths = _fetch_repo_tree(plugin_name)
    if not file_paths:
        return "(repository is empty)"

    # ── Stage 1: ask the LLM to pick relevant files ─────────────────────────
    selected_files = _search_relevant_files(plugin_name, user_query, file_paths)

    # ── Stage 2: fetch selected files ────────────────────────────────────────
    init_code = _fetch_plugin_code(plugin_name, selected_files, 2*MAX_TOTAL_CODE_CHARS//3)

    # ── Stage 3: resolve dependencies ────────────────────────────────────────
    tree_remaining = [p for p in file_paths if p not in set(selected_files)]
    dep_files = _search_relevant_files(plugin_name, init_code, tree_remaining, dep=True)

    if not dep_files:
        return init_code

    dep_code = _fetch_plugin_code(plugin_name, dep_files, MAX_TOTAL_CODE_CHARS//3)
    return init_code + "\n\n# ── Dependency files ──\n\n" + dep_code


@mcp.tool()
def plugin_code_search(plugin_name: str, query: str) -> str:
    """
    Search and analyze the source code of a Freva analysis plugin for decadal climate
    prediction. Use this when the user asks about how a plugin works, how to use it,
    or wants parts of the plugin code base to be transformed into Python code.

    Available plugins:
        cvprepare: prepares cross-validation datasets for decadal prediction skill assessment
        leadtimeselektor: extracts and aggregates lead times from decadal prediction ensembles
        problems: decadal prediction skill assessment of simumlation vs observations
        recalibration: recalibrates decadal datasets to correct model drift and biases
        terciles: computes tercile-based statistics for prediction skill assessment

    Args:
        plugin_name (str): Name of the plugin (e.g. "leadtimeselektor").
        query (str): What the user wants to know or do with the plugin.
    Returns:
        str: Relevant code context from source files of the plugin repository.
    """
    plugin_name = plugin_name.strip().lower()

    if plugin_name not in FREVA_PLUGINS:
        return (
            f"Unknown plugin '{plugin_name}'. "
            f"Available Freva plugins: {', '.join(FREVA_PLUGINS)}"
        )

    if not GITLAB_TOKEN:
        logger.error("FREVAGPT_GITLAB_TOKEN is not set; cannot access GitLab repos.")
        return "Plugin code search is unavailable (GitLab access not configured)."

    logger.info(
        "Fetching source code for plugin '%s' with query: %s", plugin_name, query
    )

    try:
        code_content = _collect_plugin_context(plugin_name, query)
    except Exception as e:
        logger.warning("Failed to fetch plugin code for '%s': %s", plugin_name, e)
        return f"Failed to retrieve source code for plugin '{plugin_name}': {e}"

    header = (
        f"Source code of the '{plugin_name}' plugin "
        f"(https://gitlab.dkrz.de/{PLUGIN_GROUP_PATH}/{plugin_name}):\n\n"
    )
    return header + code_content


def debug():
    question = "How do I submit a job to the DKRZ HPC?"
    resp = web_search(question)
    print(resp)
