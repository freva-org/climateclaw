import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote as urlquote

import httpx
from fastmcp import FastMCP
from openai import OpenAI

from climateclaw.core.logging_setup import configure_logging
from climateclaw.tools.header_gate import make_header_gate

logger = configure_logging(__name__, named_log="freva_plugin_server")

OPENAI_API_KEY: Optional[str] = os.getenv("CLIMATECLAW_OPENAI_API_KEY")
GITLAB_ACCESS_TOKEN: Optional[str] = os.getenv("CLIMATECLAW_GITLAB_ACCESS_TOKEN")

HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8053"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path

# ── Config ───────────────────────────────────────────────────────────────────
PLUGIN_SEARCH_MODEL = "gpt-5.4-mini"
GITLAB_BASE_URL = "https://gitlab.dkrz.de/api/v4"
FREVA_PROJECT_NAMES = {
    "coming decade": "kd1418",
    "climxtreme": "bm1159",
    "regiklim": "ch1187",
    "freva": "freva",
}
FREVA_PROJECTS = [
    "Coming Decade",
    "ClimXtreme",
    "RegiKlim",
    "Freva",
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
PLUGIN_TOOL_USERNAME = "username"


# ─── App ────────────────────────────────────────────────────────────────────
# Per-request header context
user_ctx: ContextVar[str | None] = ContextVar("user_ctx", default=None)

mcp = FastMCP("plugin-code-search-server")
logger.info("Starting Freva-Plugin Code Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[user_ctx],
    header_name_list=[PLUGIN_TOOL_USERNAME],
    logger=logger,
    mcp_path=PATH,
)
client = OpenAI(api_key=OPENAI_API_KEY)


def _get_user():
    user = user_ctx.get()
    if not user:
        logger.warning(f"Missing required header '{PLUGIN_TOOL_USERNAME}'! ")
        return "unknown_user"
    else:
        return user


# ── GitLab helpers ────────────────────────────────────────────────
_gitlab_http = httpx.Client(
    base_url=GITLAB_BASE_URL,
    headers={"PRIVATE-TOKEN": GITLAB_ACCESS_TOKEN or ""},
    timeout=30.0,
)


def _fetch_file_raw(project_id: int, file_path: str, branch: str) -> str:
    encoded_path = urlquote(file_path, safe="")
    resp = _gitlab_http.get(
        f"/projects/{project_id}/repository/files/{encoded_path}/raw",
        params={"ref": branch},
    )
    resp.raise_for_status()
    return resp.text


def _has_read_access(project_id: int, username: str) -> bool:
    """
    Check if the given user has at least read access rights to the GitLab project,
    based on the project's visibility, the user's ID as well as membership status.
    """
    # Get project visibility: "public", "internal", or "private"
    resp = _gitlab_http.get(f"/projects/{project_id}")
    resp.raise_for_status()
    visibility = resp.json().get("visibility")
    # Public: everyone can read
    if visibility == "public":
        return True

    # Internal: all authenticated (non-external) users can read
    resp = _gitlab_http.get("/users", params={"username": username})
    resp.raise_for_status()
    users = resp.json()
    user_id = users[0].get("id") if users else None
    if user_id is None:
        return False

    if visibility == "internal":
        return True

    # Private: must have explicit membership
    resp = _gitlab_http.get(f"/projects/{project_id}/members/all/{user_id}")
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return resp.json().get("access_level") >= 10  # Guest and above can read


def get_project_id(plugin: str, project: str) -> int | None:
    """Fetch the GitLab project ID for the given plugin name."""
    encoded = urlquote(f"{project}/plugins4freva/{plugin}", safe="")
    resp = _gitlab_http.get(f"/projects/{encoded}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("id")


def fetch_repo_tree(project_id: int) -> list[str]:
    """
    Return the recursive file tree for a plugin repository as a list of
    file paths (strings) that match relevant extensions.
    """
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
        if entry["type"] == "blob" and entry["path"].endswith(ALLOWED_FILE_EXTENSIONS)
    ]
    return paths


def fetch_plugin_code(
    project_id: int, selected_files: list[str], max_chars: int
) -> str:
    """
    Fetch the raw content of selected files and concatenate them into a single string,
    until the total character count reaches `max_chars`.
    The default branch name for all files is "levante" or "master".
    """
    # first get the repo's default path
    resp = _gitlab_http.get(f"/projects/{project_id}")
    resp.raise_for_status()
    default_branch = resp.json().get("default_branch")

    collected: list[str] = []
    total_chars = 0
    for file in selected_files:
        if total_chars >= max_chars:
            collected.append(f"\n--- (truncated: reached {max_chars} char limit) ---")
            break
        try:
            content = _fetch_file_raw(project_id, file, default_branch)
            if len(content) > MAX_FILE_SIZE_BYTES:
                content = content[:MAX_FILE_SIZE_BYTES] + "\n... (file truncated)"
            collected.append(f"### FILE: {file} ###\n```\n{content}\n```\n")
            total_chars += len(content)
        except Exception as e:
            logger.debug(
                "Skipping file %s due to error in fetching content: %s", file, e
            )

    if not collected:
        return "(no source files could be retrieved)"
    return "\n".join(collected)


# ──────────────────────────────────────────────────────────────────────────


def select_relevant_files(
    plugin: str, query: str, file_paths: list[str], dep: bool = False
) -> list[str]:
    """
    Given a list of file paths in the plugin repo, ask the model to pick the most relevant
    ones for the user's query (dep=False) or for searching code dependencies (dep=True).
    If the LLM-based selection fails, fall back to a heuristic of picking all files.
    """
    file_tree = "\n".join(file_paths)
    if not dep:
        selection_prompt = (
            f"Task: You are selecting the most relevant files from the '{plugin}' Freva plugin repository.\n\n"
            "Selection rules:\n"
            "- Prioritize files that seem most relevant to answer the query intent.\n"
            "- For high level usage/configuration questions, prioritize wrapper/config files and README/docs.\n"
            "- For questions about implementation logic, prioritize core source code modules.\n"
            "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
            f"- Return ONLY a valid JSON array of file path strings from the provided list, with at most {MAX_RELEVANT_FILES} items. Output nothing but the JSON array."
            f"Repository file list:\n{file_tree}\n\n"
            f"User query:\n{query}\n\n"
        )
    else:
        selection_prompt = (
            "Task: You are analyzing Python source code from a repository. "
            " Your job is to find which repository files are directly imported by the given source code.\n\n"
            "Selection rules:\n"
            "- Scan the code for all import statements (import X, from X import Y).\n"
            "- Match each import to a file in the repository list using Python module path conventions (e.g. 'from foo.bar import baz' maps to 'foo/bar.py').\n"
            "- Only include files that are directly imported — do NOT infer transitive dependencies.\n"
            "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
            f"- Return ONLY a valid JSON array of at most {MAX_RELEVANT_FILES} file path strings "
            "from that list that are imported or depend upon the fetched code. "
            "If none match, return []."
            f"=== Fetched Code ===\n{query}\n=== END ===\n\n"
            f"=== Remaining Repository Files ===\n{file_tree}\n=== END ===\n\n"
        )

    try:
        selection_resp = client.responses.create(
            model=PLUGIN_SEARCH_MODEL,
            input=[{"role": "user", "content": selection_prompt}],
            stream=False,
        )
        raw_text = selection_resp.output_text.strip()
        logger.debug(
            "LLM file selection response for plugin '%s': %s", plugin, raw_text
        )
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


def collect_plugin_context(plugin: str, project_id: int, user_query: str) -> str:
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
            "%s retrieval stage selected %d/%d files for plugin '%s': %s",
            stage,
            len(files),
            len(file_paths),
            plugin,
            files,
        )

    # ── Stage 0: fetch the repository tree with all files ────────────────────
    file_paths = fetch_repo_tree(project_id)
    if not file_paths:
        return "(repository is empty)"

    # ── Stage 1: ask the LLM to select relevant files ─────────────────────────
    base_files = select_relevant_files(plugin, user_query, file_paths)
    _log_stage("Initial", base_files)

    # ── Stage 2: fetch selected files ────────────────────────────────────────
    init_code = fetch_plugin_code(project_id, base_files, 2 * MAX_TOTAL_CODE_CHARS // 3)

    # ── Stage 3: resolve dependencies ────────────────────────────────────────
    tree_remaining = [p for p in file_paths if p not in set(base_files)]
    dep_files = select_relevant_files(plugin, init_code, tree_remaining, dep=True)
    _log_stage("Dependency", dep_files)
    if not dep_files:
        return init_code

    dep_code = fetch_plugin_code(project_id, dep_files, MAX_TOTAL_CODE_CHARS // 3)
    return init_code + "\n\n# ── Dependency files ──\n\n" + dep_code


def validate_plugin_call(
    plugin: str, project: str, project_id: int | None
) -> Tuple[bool, str]:
    """
    Validate the project and plugin names, and check if GitLab repo access for the current
    user is configured for reading rights.
    Returns a tuple (bool, str):
    - False and an error message string if validation fails; or
    - True and a success message if valid.
    """
    # validate project name
    if not project:
        error_msg = (
            f"Unknown project '{project}'. "
            f"Available projects: {', '.join(FREVA_PROJECTS)}"
        )
        return False, error_msg

    # validate GitLab access of user
    if project_id is None:
        error_msg = f"Plugin '{plugin}' not found in GitLab project '{project}'."
        return False, error_msg

    # Username hardcoded for now; replace with _get_user() for production
    user_name = "k202218"
    # user_name = _get_user()
    try:
        user_access = _has_read_access(project_id, user_name)
        if not user_access:
            logger.warning(
                "User '%s' does NOT have read access to plugin '%s' in project '%s'.",
                user_name,
                plugin,
                project,
            )
            error_msg = (
                f"User access for {user_name} to plugin '{plugin}' denied! "
                f"Get access by being added to GitLab project '{project}'."
            )
            return False, error_msg
        logger.info(
            "Authorization layer passed: User has read access to plugin '%s' in project '%s'.",
            plugin,
            project,
        )
    except httpx.HTTPError as e:
        logger.error("Error checking GitLab repo membership: %s", e)
        error_msg = "Plugin code search is currently unavailable (GitLab access error)."
        return False, error_msg

    return True, "Validation successful"


def auto_detect_plugin_project(user_query: str) -> tuple[str, str]:
    """
    Attempt to automatically detect the plugin and project names from the user's query
    by making a call to the LLM.

    Returns a tuple of (plugin_name, project_name):
    - plugin_name (str): Name of the repo or plugin (e.g. "leadtimeselektor")
    - project_name (str): Name of the Freva instance/project
    """
    file_path = Path(__file__).parent / "available_plugins.md"
    plugin_descriptions = file_path.read_text(encoding="utf-8", newline="\n")

    selection_prompt = (
        "Task: Select the single best matching Freva plugin & project name for the user query from the provided plugin summaries.\n"
        "Rules:\n"
        "- Use only plugin/project names that appear in the summaries.\n"
        "- Return exactly one plugin and its corresponding project.\n"
        "- If unsure, choose the closest semantic match.\n"
        "- Output must be exactly this format with no extra text: <plugin_name>,<project_name>\n\n"
        f"User query:\n{user_query}\n\n"
        f"Available plugin summaries:\n{plugin_descriptions}"
    )
    selection_resp = client.responses.create(
        model=PLUGIN_SEARCH_MODEL,
        input=[{"role": "user", "content": selection_prompt}],
        stream=False,
    )

    plugin_name, project_name = selection_resp.output_text.strip().split(",")
    logger.info(
        "Auto-detected plugin '%s' and project '%s' for user query.",
        plugin_name,
        project_name,
    )
    return plugin_name, project_name


@mcp.tool()
def plugin_code_search(user_query: str) -> str:
    """
    Search and analyze the source code of a Freva data analysis plugin for decadal climate
    predictions. Use this when the user
    - explicitly asks how a plugin's internal logic works, how to run or
    configure it, or when code snippets from the plugin should be translated or
    adapted into Python examples;
    - asks general questions about decadal climate prediction analysis, where repository-grounded code context could be used to answer the question.

    Args:
        user_query (str): What the user wants to know about or do w.r.t. decadal climate prediction plugins.

    Returns:
        str: Relevant code context fetched from source files of the plugin repository;
        or an error message if the plugin call is not authorized / code retrieval fails.
    """
    plugin_name, project_name = auto_detect_plugin_project(user_query)
    project = FREVA_PROJECT_NAMES.get(project_name.strip().lower(), "")
    plugin = plugin_name.strip().lower()

    # Validate the plugin call
    project_id = get_project_id(plugin, project)
    result, message = validate_plugin_call(plugin, project, project_id)
    if not result:
        return message

    # Fetch the plugin code and return it with a header
    logger.info(
        "Fetching source code for plugin '%s' with query: %s", plugin, user_query
    )
    try:
        code_content = collect_plugin_context(plugin, project_id, user_query)  # type: ignore
    except Exception as e:
        logger.warning("Failed to fetch plugin code for '%s': %s", plugin, e)
        return f"Failed to retrieve source code for plugin '{plugin}': {e}"

    header = (
        f"Relevant retrieved code of the '{plugin}' plugin "
        f"(https://gitlab.dkrz.de/{project}/plugins4freva/{plugin}):\n\n"
    )
    return header + code_content
