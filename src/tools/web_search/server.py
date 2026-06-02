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
WEB_SEARCH_MODEL = "gpt-5.4-mini"
ALLOWED_DOMAINS = [
    "docs.dkrz.de",
    "docs.icon-model.org",
]

# GitLab plugin config
GITLAB_BASE_URL = "https://gitlab.dkrz.de/api/v4"
FREVA_PROJECT_NAMES = {"codes": "kd1418", "xces": "bm1159", "regiklim": "ch1187"}
FREVA_PLUGINS = [
    "cvprepare",
    "leadtimeselektor",
    "problems",
    "recalibration",
    "terciles",
]
ALLOWED_FILE_EXTENSIONS = (
    ".py",
    ".sh",
    ".R",
    ".md",
    ".rst",
)  # only fetch these file types
MAX_FILE_SIZE_BYTES = 50_000  # skip files larger than this
MAX_TOTAL_CODE_CHARS = 50_000  # truncate total fetched code after this limit
MAX_RELEVANT_FILES = 3  # max files to fetch for relevance filtering

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
        "Searching for DKRZ/HPC- or ICON-related context in documentation " f"for query: {query}"
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
            {"role": "user", "content": user_query},
        ],
        "stream": False,
        "tool_choice": "auto",
        "tools": [{"type": "web_search", "filters": {"allowed_domains": ALLOWED_DOMAINS}}],
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
# ── GitLab helpers ────────────────────────────────────────────────
_gitlab_http = httpx.Client(
    base_url=GITLAB_BASE_URL,
    headers={"PRIVATE-TOKEN": GITLAB_TOKEN or ""},
    timeout=30.0,
)


def _gitlab_project_id(project: str, plugin: str) -> str:
    """URL-encoded project path usable as :id in the GitLab v4 API."""
    return urlquote(f"{project}/plugins4freva/{plugin}", safe="")


def fetch_repo_tree(project: str, plugin: str) -> list[str]:
    """
    Return the recursive file tree for a plugin repository as a list of
    file paths (strings) that match relevant extensions.
    """
    project_id = _gitlab_project_id(project, plugin)
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
        entry["path"]
        for entry in items
        if entry["type"] == "blob" and entry["path"].lower().endswith(ALLOWED_FILE_EXTENSIONS)
    ]
    return paths


def fetch_plugin_code(project: str, plugin: str, selected_files: list[str], max_chars: int) -> str:
    """
    Fetch the raw content of selected files and concatenate them into a single string,
    until the total character count reaches `max_chars`.
    The default branch name for all files is "levante".
    """

    def _fetch_file_raw(file_path: str, ref: str = "levante") -> str:
        project_id = _gitlab_project_id(project, plugin)
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
            collected.append(f"\n--- (truncated: reached {max_chars} char limit) ---")
            break
        try:
            content = _fetch_file_raw(file)
            if len(content) > MAX_FILE_SIZE_BYTES:
                content = content[:MAX_FILE_SIZE_BYTES] + "\n... (file truncated)"
            collected.append(f"### FILE: {file} ###\n```\n{content}\n```\n")
            total_chars += len(content)
        except Exception as e:
            logger.debug("Skipping file %s: %s", file, e)

    if not collected:
        return "(no source files could be retrieved)"

    return "\n".join(collected)


# ──────────────────────────────────────────────────────────────────────────


def search_relevant_files(
    plugin: str, query: str, file_paths: list[str], dep: bool = False
) -> list[str]:
    """
    Given a list of file paths in the plugin repo, ask the model to pick the most relevant
    ones for the user's query (dep=False) or for searching code dependencies (dep=True).
    If the LLM-based selection fails, fall back to a heuristic of picking all files.
    """
    if not dep:
        tree_listing = "\n".join(file_paths)
        selection_prompt = (
            f"You are analyzing the '{plugin}' Freva plugin repository, "
            f"which contains the following files:\n{tree_listing}\n\n"
            f'The user\'s query now is:\n"{query}"\n\n'
            "Return ONLY a JSON array of file paths (strings) that seem most relevant to "
            "answering the user's query, but do not include test files. "
            "For high-level questions about documentation, focus more on README / documentation"
            "files; whereas for implementation details, focus more on source code files. "
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
        logger.debug("LLM file selection response for plugin '%s': %s", plugin, raw_text)
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[1:])
        if raw_text.endswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[:-1])
        selected_files: list[str] = json.loads(raw_text.strip())
    except Exception as e:
        logger.warning("LLM file selection failed (%s); falling back to heuristic.", e)
        # Fallback: pick all files from the tree search;
        # or pick nothing for dependency selection to avoid fetching duplicate code
        selected_files = file_paths if not dep else []

    # Return only paths that actually exist in the tree
    return [p for p in selected_files if p in set(file_paths)][:MAX_RELEVANT_FILES]


def collect_plugin_context(project: str, plugin: str, user_query: str) -> str:
    """
    Three-stage context retrieval of code base:
        1. Ask LLM which files are most relevant for the user's query.
        2. Fetch those files, then scan for imports to identify dependent modules.
        3. Fetch the dependencies and return the combined contents.
    Returns a string containing the concatenated relevant code snippets,
        separated by file and with a header.
    """

    def _log_stage(stage: str, files: list[str]):
        logger.info(
            "%s retrieval selected %d/%d files for plugin '%s': %s",
            stage,
            len(files),
            len(file_paths),
            plugin,
            files,
        )

    # ── Stage 0: fetch the repository tree with all files ────────────────────
    file_paths = fetch_repo_tree(project, plugin)
    if not file_paths:
        return "(repository is empty)"

    # ── Stage 1: ask the LLM to pick relevant files ─────────────────────────
    base_files = search_relevant_files(plugin, user_query, file_paths)
    _log_stage("Initial", base_files)

    # ── Stage 2: fetch selected files ────────────────────────────────────────
    init_code = fetch_plugin_code(project, plugin, base_files, 2 * MAX_TOTAL_CODE_CHARS // 3)

    # ── Stage 3: resolve dependencies ────────────────────────────────────────
    tree_remaining = [p for p in file_paths if p not in set(base_files)]
    dep_files = search_relevant_files(plugin, init_code, tree_remaining, dep=True)
    _log_stage("Dependency", dep_files)
    if not dep_files:
        return init_code

    dep_code = fetch_plugin_code(project, plugin, dep_files, MAX_TOTAL_CODE_CHARS // 3)
    return init_code + "\n\n# ── Dependency files ──\n\n" + dep_code


def validate_plugin_call(project: str, plugin: str) -> Optional[str]:
    """
    Validate the project and plugin names, and check if GitLab access is configured.
    Returns an error message string if validation fails, or None if valid.
    """
    # validate project and plugin names
    if project not in FREVA_PROJECT_NAMES.values():
        return (
            f"Unknown project '{project}'. "
            f"Available projects: {', '.join(FREVA_PROJECT_NAMES.values())}"
        )
    if plugin not in FREVA_PLUGINS:
        return (
            f"Unknown plugin '{plugin}'. " f"Available Freva plugins: {', '.join(FREVA_PLUGINS)}"
        )

    # validate GitLab access of user
    if not GITLAB_TOKEN:
        return "Plugin code search is unavailable (GitLab access not configured)."

    # Check user membership in the project's GitLab group
    # TODO: replace with actual user identification from backend container
    user = "unknown_user"
    try:
        encoded_group = urlquote(project, safe="")
        resp = _gitlab_http.get(f"/groups/{encoded_group}/members", params={"per_page": 100})
        resp.raise_for_status()
        members = resp.json()
        member_usernames = [m.get("username") for m in members]
        if user not in member_usernames:
            return f"User '{user}' is not a member of the GitLab group '{project}'. Access denied."
    except httpx.HTTPError as e:
        logger.error(f"Error checking GitLab group membership: {e}")
        return "Plugin code search is unavailable (GitLab access error)."


@mcp.tool()
def plugin_code_search(project_name: str, plugin_name: str, query: str) -> str:
    """
    Search and analyze the source code of a Freva data analysis plugin for decadal climate
    predictions. Use this when the user asks about how a plugin works, how to use it,
    or wants parts of the plugin code base to be transformed into Python code.

    Available plugins:
        - cvprepare: prepares cross-validation datasets for decadal prediction skill assessment
        - leadtimeselektor / leadtimeSelect: extracts and aggregates lead times from decadal prediction ensembles
        - problems: decadal prediction skill assessment of simulation vs reanalysis or observations
        - recalibration: calibrates decadal datasets to observation for model drift and bias correction
        - terciles: computes tercile-based statistics for prediction skill assessment

    Args:
        project_name (str): Name of the Freva instance/project. Available options:
            - "codes" (aka Coming Decade)
            - "xces" (aka ClimXtreme)
            - "regiklim" (aka Regional Climate Projections)
        plugin_name (str): Name of the repo or plugin (e.g. "leadtimeselektor").
        query (str): What the user wants to know about or do with the plugin.
    Returns:
        str: Relevant code context fetched from source files of the plugin repository.
    """
    project = FREVA_PROJECT_NAMES.get(project_name.strip().lower(), "")
    plugin = plugin_name.strip().lower()

    # Validate the plugin call
    warning = validate_plugin_call(project, plugin)
    if warning:
        return warning

    # Fetch the plugin code and return it with a header
    logger.info("Fetching source code for plugin '%s' with query: %s", plugin, query)
    try:
        code_content = collect_plugin_context(project, plugin, query)
    except Exception as e:
        logger.warning("Failed to fetch plugin code for '%s': %s", plugin, e)
        return f"Failed to retrieve source code for plugin '{plugin}': {e}"

    header = (
        f"Relevant retrieved code of the '{plugin}' plugin "
        f"(https://gitlab.dkrz.de/{project}/plugins4freva/{plugin}):\n\n"
    )
    return header + code_content


def debug():
    question = "How do I submit a job to the DKRZ HPC?"
    resp = web_search(question)
    print(resp)
