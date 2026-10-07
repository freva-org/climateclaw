import ast
import json
import os
import re
import textwrap
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote as urlquote

import httpx
from fastmcp import FastMCP

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.streaming.litellm_client import acomplete, first_text
from climateclaw.tools.header_gate import make_header_gate

logger = configure_logging(__name__, named_log="plugin_code_search_server")

HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8053"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path

# ── Config ───────────────────────────────────────────────────────────────────
GITLAB_ACCESS_TOKEN: Optional[str] = os.getenv("CLIMATECLAW_GITLAB_ACCESS_TOKEN")
GITLAB_BASE_URL = "https://gitlab.dkrz.de/api/v4"
FREVA_PROJECT_NAMES = {
    "coming decade": "kd1418",
    "climxtreme": "bm1159",
    "regiklim": "ch1187",
    "freva": "freva",
}
ALLOWED_FILE_EXTENSIONS = (
    ".py",
    ".sh",
    ".R",
    ".md",
    ".rst",
)  # only fetch these file types
MAX_FILE_SIZE_BYTES = 50_000  # skip files larger than this
MAX_TOTAL_CODE_CHARS = 70_000  # truncate total fetched code after this limit
MAX_FILES = 5  # max files the LLM may select in total
ENTRY_PATTERN = re.compile(r"(wrapper|api)[^/]*\.py$", re.IGNORECASE)
EXCLUDE_PATTERN = re.compile(r"(^|/)(tests?/|test_|__init__\.py$)", re.IGNORECASE)
USERNAME = "username"
MODEL = "model"


# ─── App ────────────────────────────────────────────────────────────────────
# Per-request header context
username_ctx: ContextVar[str | None] = ContextVar("username_ctx", default=None)
model_ctx: ContextVar[str | None] = ContextVar("model_ctx", default=None)

mcp = FastMCP("plugin-code-search-server")
logger.info("Starting Freva-Plugin Code Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[username_ctx, model_ctx],
    header_name_list=[USERNAME, MODEL],
    logger=logger,
    mcp_path=PATH,
)


def _get_user():
    user = username_ctx.get()
    if not user:
        logger.warning(f"Missing required header '{USERNAME}'! ")
        return "unknown_user"
    else:
        return user


def _get_model():
    model = model_ctx.get()
    if not model:
        logger.warning(f"Missing required header '{MODEL}'! ")
        return MODEL
    else:
        return model


async def call_llm(prompt: str) -> str:
    model = _get_model()
    resp = await acomplete(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = first_text(resp).strip()
    return raw_text


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


def fetch_repo_tree_and_branch(project_id: int) -> tuple[list[str], str]:
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

    # Get the default branch of the repository (usually "levante" or "master")
    resp = _gitlab_http.get(f"/projects/{project_id}")
    resp.raise_for_status()
    default_branch = resp.json().get("default_branch")
    return paths, default_branch


def fetch_files(
    project_id: int, branch: str, selected_files: list[str], max_chars: int
) -> dict[str, str]:
    """
    Fetch the raw content of selected files until the total character count
    reaches `max_chars`. Returns a mapping of file path -> content.
    """
    collected: dict[str, str] = {}
    budget = max_chars
    for file in selected_files:
        if budget <= 0:
            break
        try:
            content = _fetch_file_raw(project_id, file, branch)
        except Exception as e:
            logger.debug(
                "Skipping file %s due to error in fetching content: %s", file, e
            )
            continue
        if len(content) > MAX_FILE_SIZE_BYTES:
            content = content[:MAX_FILE_SIZE_BYTES] + "\n... (file truncated)"
        if len(content) > budget:
            content = (
                content[:budget] + f"\n... (truncated: reached {max_chars} char limit)"
            )
        collected[file] = content
        budget -= len(content)
    return collected


# ── Other helpers ────────────────────────────────────────────────


def extract_method_from_source(
    source_code: str, method_name: str = "run_tool"
) -> str | None:
    """Parses a Python source string and returns the code of a specific method."""
    try:
        # 1. Clean tabs and fix uneven leading indentation
        cleaned_code = source_code.expandtabs(4)
        cleaned_code = textwrap.dedent(cleaned_code)

        # 2. Parse the cleaned source code
        tree = ast.parse(cleaned_code)

        # Look for the method definition anywhere in the tree
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                # ast.get_source_segment extracts the exact string from the source
                method_code = ast.get_source_segment(source_code, node)
                return method_code

        return f"Method '{method_name}' not found in the source code."

    except SyntaxError as e:
        return f"Syntax error in the provided source code: {e}"


def format_files(files: dict[str, str]) -> str:
    """Concatenate fetched files into a single string, separated by file headers."""
    return "\n".join(
        f"### FILE: {path} ###\n```\n{content}\n```\n"
        for path, content in files.items()
    )


# ──────────────────────────────────────────────────────────────────────────


def find_entry_file(file_paths: list[str]) -> str:
    """
    Deterministically find the plugin entry file ('*wrapper*.py' or '*api*.py'),
    excluding tests. Shallower paths come first, as they are most likely the
    actual plugin entry point.
    """
    matches = [
        p
        for p in file_paths
        if ENTRY_PATTERN.search(p) and not EXCLUDE_PATTERN.search(p)
    ]
    return sorted(matches, key=lambda p: (p.count("/"), p))[0]


async def _llm_select_files(
    prompt: str, file_paths: list[str], num_files: int, fallback: list[str]
) -> list[str]:
    """
    Ask the LLM for a JSON array of file paths and keep only those that exist
    in `file_paths`. Returns `fallback` if the LLM response cannot be parsed.
    """
    try:
        raw_text = await call_llm(prompt)
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[1:])
        if raw_text.endswith("```"):
            raw_text = "\n".join(raw_text.split("\n")[:-1])
        select_files: list[str] = json.loads(raw_text.strip())
    except Exception as e:
        logger.warning("LLM file selection failed (%s); falling back to heuristic.", e)
        select_files = fallback

    # Return only paths that exist in repo tree and conform to exclusion pattern
    valid_paths = [
        p
        for p in select_files
        if p in set(file_paths) and not EXCLUDE_PATTERN.search(p)
    ]
    return valid_paths[:num_files]  # limit to max number of files


async def select_init_files(
    plugin: str,
    user_query: str,
    fetched_code: str,
    file_paths: list[str],
    num_files: int,
) -> list[str]:
    """
    Fallback if no entry file was found: let the LLM pick the most relevant files
    from the repo tree for the user's query.
    """
    file_tree = "\n".join(file_paths)
    selection_prompt = (
        f"Task: You are analyzing the files from the '{plugin}' Freva plugin repository. "
        "Given the context and module imports in the fetched code, prioritize and select the files that seem *most* relevant to answer the user's query.\n\n"
        "Selection rules:\n"
        "- For high level usage/configuration questions, prioritize wrapper/config files, README and docs.\n"
        "- For questions about implementation logic, prioritize source code modules.\n"
        "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
        f"- Return ONLY a valid JSON array of at most {num_files} file path strings from the provided list. Output nothing but the JSON array.\n\n"
        f"=== User Query ===\n{user_query}\n=== END ===\n\n"
        f"=== Repository file list ===\n{file_tree}\n=== END ===\n\n"
        f"=== Fetched Code ===\n{fetched_code}\n=== END ===\n\n"
    )
    # Fallback: pick all files from the tree search
    return await _llm_select_files(
        selection_prompt, file_paths, num_files, fallback=file_paths
    )


async def select_dependency_files(
    plugin: str,
    user_query: str,
    fetched_code: str,
    file_paths: list[str],
    num_files: int,
) -> list[str]:
    """
    Ask the LLM which of the remaining repo files are direct dependencies of the
    fetched code.
    """
    file_tree = "\n".join(file_paths)
    selection_prompt = (
        f"Task: You are tracing the code base of the '{plugin}' Freva plugin. "
        "Given the already fetched source code, prioritize and select which of the remaining repository files should be read next.\n\n"
        "Selection rules:\n"
        "- Scan the fetched code for imports or script calls and map each to the matching repository file using Python module conventions "
        "(e.g., 'from foo.bar import baz' -> 'foo/bar.py').\n"
        "- Ignore imports from third-party libraries (e.g. numpy, xarray, evaluation_system etc.) and focus on the plugin's own source code.\n"
        "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
        f"- Return ONLY a valid JSON array of at most {num_files} file path strings from the remaining repository list. If none match, return []. Output nothing but the JSON array.\n\n"
        f"=== User Query ===\n{user_query}\n=== END ===\n\n"
        f"=== Fetched Code ===\n{fetched_code}\n=== END ===\n\n"
        f"=== Remaining Repository Files ===\n{file_tree}\n=== END ===\n\n"
    )
    # Fallback: pick nothing to avoid fetching irrelevant code
    return await _llm_select_files(selection_prompt, file_paths, num_files, fallback=[])


async def collect_plugin_context(
    plugin: str, project: str, project_id: int, user_query: str
) -> str:
    """
    Entry-point-driven context retrieval of the plugin code base:
        1. Deterministically find the plugin entry files ('*wrapper*.py' / '*api*.py')
           in the repo tree.
        2. Let the LLM select the direct dependencies of the entry files plus other
           modules relevant for the user's query, and fetch those.
        3. Let the LLM resolve the direct dependencies of the newly fetched files.
    Returns a string containing the concatenated relevant source code files,
        separated by file and with a header.
    """

    def _log_stage(stage: str, files: list[str]):
        logger.info(
            "%s retrieval step selected %d/%d files for plugin '%s': %s",
            stage,
            len(files),
            len(file_paths),
            plugin,
            files,
        )

    header = (
        f"Relevant retrieved code of the '{plugin}' plugin "
        f"(https://gitlab.dkrz.de/{project}/plugins4freva/{plugin}):\n\n"
    )

    # ── Fetch the repository tree with all files ────────────────────
    file_paths, branch = fetch_repo_tree_and_branch(project_id)
    if not file_paths:
        return f"repository is empty for plugin '{plugin}' in branch '{branch}'"

    # ── Stage 1: find & fetch plugin entry ─────────────────────
    entry_file = find_entry_file(file_paths)
    _log_stage("Deterministic entry file", [entry_file])

    exec_code = extract_method_from_source(
        _fetch_file_raw(project_id, entry_file, branch), method_name="run_tool"
    )
    exec_code = exec_code or "No 'run_tool' method found in the entry file."
    _log_stage("Run_tool method extraction", [exec_code])
    entry_code = format_files({entry_file: exec_code})

    # ── Stage 2: find & fetch useful files, based on entry file + user context ───────
    remaining = [p for p in file_paths if p not in entry_file]
    init_files = await select_init_files(
        plugin, user_query, entry_code, remaining, MAX_FILES
    )
    init_files = list(set([entry_file] + init_files))
    init_code = fetch_files(project_id, branch, init_files, MAX_TOTAL_CODE_CHARS)
    _log_stage("Combined LLM-based initial", init_files)

    # ── Stage 3: resolve dependencies ───────────────────────────────────
    budget = MAX_TOTAL_CODE_CHARS - sum(len(c) for c in init_code.values())
    remaining = [p for p in file_paths if p not in init_code]
    max_deps = MAX_FILES - len(init_files)
    dep_files = await select_dependency_files(
        plugin,
        user_query,
        format_files(init_code),
        remaining,
        max_deps,
    )
    _log_stage("LLM-based dependency", dep_files)
    deps_code = fetch_files(project_id, branch, dep_files, budget)

    # Format the final output with header and fetched code, including dependencies
    code_content = format_files(init_code)
    if deps_code:
        code_content += "\n\n### ── Dependency files ── ###\n\n" + format_files(
            deps_code
        )
    return header + code_content


def validate_plugin_call(
    plugin: str, project: str, project_id: int | None
) -> Tuple[bool, str]:
    """
    Validate GitLab repo availability for chosen plugin / project names,
    as well as reading access for the current user.
    Returns a tuple (bool, str):
    - False and an error message string if validation fails; or
    - True and a success message if valid.
    """
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


async def detect_plugin_project(user_query: str) -> tuple[str, str]:
    """
    Attempt to automatically detect the plugin and project names from the user's query
    by making a call to the LLM.

    Returns a tuple of (plugin_name, project_name):
    - plugin_name (str): Name of the repo or plugin (e.g. "leadtimeselektor")
    - project_name (str): Name of the Freva instance (e.g. "kd1418" for "coming decade")
    """
    file_path = Path(__file__).parent / "available_plugins.md"
    plugin_descriptions = file_path.read_text(encoding="utf-8", newline="\n")
    # let LLM select plugin and project names from the available plugin summaries
    selection_prompt = (
        "Task: Select the semantically best matching Freva plugin & project name for the user query from the provided plugin summaries.\n"
        "Rules:\n"
        "- Select only plugin & project names that appear in the summaries.\n"
        "- Return exactly one plugin and its corresponding project.\n"
        "- Output must be exactly this format with no extra text: <plugin_name>,<project_name>\n\n"
        f"User query:\n{user_query}\n\n"
        f"Available plugin summaries:\n{plugin_descriptions}"
    )
    raw_text = await call_llm(selection_prompt)
    # format to "<plugin_name>,<project_name>" output
    plugin_name, project_name = raw_text.split(",", maxsplit=1)
    plugin_name = plugin_name.strip().lower()
    project_name = FREVA_PROJECT_NAMES.get(project_name.strip().lower(), "")
    logger.info(
        "Auto-detected plugin '%s' and project '%s' for examining code base.",
        plugin_name,
        project_name,
    )
    return plugin_name, project_name


@mcp.tool()
async def plugin_code_search(user_query: str) -> str:
    """
    Fetch relevant source code and documentation files of a Freva data analysis plugin
    as a repository-grounded code knowledge base.

    Use this when the user
    - explicitly asks how a plugin's internal logic works, how to run or configure it,
    or wants plugin code translated or adapted into Python examples;
    - asks a specific climate or weather question involving regional, decadal, or
    extreme-event analysis (lead time selection, hindcast skill scoring, bias adjustment
    and drift correction, downscaling, precipitation indices, crop impact, heat waves/HWMID
    climate/extreme indices, region matching, urban heatwave extraction, precipitation
    disaggregation, and plugin creation).

    Workflow Guidelines:
    - Ground explanations only in returned code context.
    - Handle retrieved plugin code in two separate steps:
      1. **First step (always) – only high level:** a factful explanation of how the plugin works and how to use it (including plugin and project names).
      2. **Second step (only if requested) – implementation:** turn the plugin's *core logic* into a lightweight Python snippet with a concise plan.
    - reference modules, classes, and functions only when asked for more detail.
    - call it again for follow-up questions *not* sufficiently covered by prior results.
    - only implement the plugin's *core logic* in a lightweight Python snippet - for that:
        - replace `cdo` with `xarray` operations
        - prioritize workflow correctness over mirroring non-critical details (e.g. multiple tries, fallbacks, logging)

    Args:
    -----
        user_query (str): What the user wants to know about or do with a dedicated Freva
        plugin regarding climate or weather data analysis. Always the only argument.

    Returns:
    --------
        str: Relevant code context (with a header containing the plugin's repo URL),
        including directly imported dependency files; or an error message if the plugin
        is not found, user access is denied, or code retrieval fails.

    Examples:
    ---------
    - "How does the 'leadtimeselektor' plugin work from a high-level perspective?"
    - "How can I calculate climate prediction skill scores against observations or reanalysis data?"
    - "How can I assess the impact of extreme climate events on crop productivity?"
    """
    plugin, project = await detect_plugin_project(user_query)
    # return f"plugin_name: {plugin}, project_name: {project}"  # only for benchmarking the plugin detection stage

    # Validate the plugin call
    project_id = get_project_id(plugin, project)
    result, message = validate_plugin_call(plugin, project, project_id)
    if not result:
        return message

    # Fetch the plugin code and return it with a header
    logger.info("Fetching source code for plugin '%s'", plugin)
    try:
        return await collect_plugin_context(plugin, project, project_id, user_query)  # type: ignore
    except Exception as e:
        logger.warning("Failed to fetch plugin code for '%s': %s", plugin, e)
        return f"Failed to retrieve source code for plugin '{plugin}': {e}"
