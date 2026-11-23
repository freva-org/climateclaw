import ast
import asyncio
import json
import os
import re
import textwrap
from contextvars import ContextVar
from pathlib import Path
from typing import Tuple
from urllib.parse import quote as urlquote
from urllib.parse import unquote, urlsplit

import httpx
from async_lru import alru_cache
from fastmcp import FastMCP

from climateclaw.core.available_chatbots import available_chatbots, default_chatbot
from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.streaming.litellm_client import acomplete, first_text
from climateclaw.tools.active_requests import ACTIVE_REQUESTS
from climateclaw.tools.header_gate import RequestIdMiddleware, make_header_gate
from climateclaw.tools.models import GenericToolResult

logger = configure_logging(__name__)


# ── Tool Config ────────────────────────────────────────────────────────────────
GITLAB_ACCESS_TOKEN = os.getenv("CLIMATECLAW_GITLAB_ACCESS_TOKEN")
GITLAB_BASE_URL = "https://gitlab.dkrz.de/api/v4"
GITLAB_CACHE_SECONDS = 600

FREVA_PROJECT_NAMES = {
    "coming decade": "kd1418",
    "climxtreme": "bm1159",
    "regiklim": "ch1187",
    "freva": "freva",
}
ALLOWED_FILE_EXTENSIONS = (
    ".md",
    ".py",
    ".R",
    ".rst",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
)  # only fetch these file types
MAX_FILE_SIZE_BYTES = 50_000  # skip files larger than this
MAX_TOTAL_CODE_CHARS = 100_000  # truncate total fetched code after this limit
MAX_FILES = 6  # max files the LLM may select in total
ENTRY_PATTERN = re.compile(r"(wrapper|api)[^/]*\.py$", re.IGNORECASE)
EXCLUDE_PATTERN = re.compile(r"(^|/)(tests?/|test_|__init__\.py$)", re.IGNORECASE)


# ── MCP Server Config ───────────────────────────────────────────────────────
HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8053"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path
USERNAME = "username"
MODEL = "deepseek-v4.1"
MODEL = MODEL if MODEL in available_chatbots() else default_chatbot()


# ─── App ────────────────────────────────────────────────────────────────────
mcp = FastMCP("plugin-code-search-server")
mcp.add_middleware(RequestIdMiddleware())
logger.info("Starting Freva-Plugin Code Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Per-request header context
username_ctx: ContextVar[str | None] = ContextVar("username_ctx", default=None)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[username_ctx],
    header_name_list=[USERNAME],
    logger=logger,
    mcp_path=PATH,
    on_cancel_request=ACTIVE_REQUESTS.cancel,
)


def _get_user():
    user = username_ctx.get()
    if not user:
        logger.error(f"Missing required header '{USERNAME}'! ")
        return "unknown_user"
    else:
        return user


async def call_llm(prompt: str) -> str:
    """Call the language model with the given prompt and return the response as a string."""
    for attempt in range(2):
        try:
            resp = await acomplete(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = first_text(resp).strip()
            return raw_text
        except Exception as e:
            await asyncio.sleep(5)  # brief pause before retrying
            logger.error("Error during LLM call: %s", e)
            if attempt == 1:
                return ""
    return ""


# ── GitLab helpers ────────────────────────────────────────────────
_gitlab_http = httpx.AsyncClient(
    base_url=GITLAB_BASE_URL,
    headers={"PRIVATE-TOKEN": GITLAB_ACCESS_TOKEN or ""},
    timeout=30.0,
)


async def gitlab_healthcheck() -> bool:
    """Check if the GitLab server is reachable and the access token is valid."""
    try:
        resp = await _gitlab_http.get("/projects", params={"per_page": 1})
        resp.raise_for_status()
        logger.info("GitLab startup check succeeded.")
        return True
    except Exception as e:
        logger.error(
            "GitLab startup check failed. "
            "GitLab server is not reachable or access token has become invalid: %s",
            e,
        )
        return False


async def fetch_file_raw(
    project_id: int, file_path: str, branch: str, max_bytes: int | None = None
) -> str:
    """
    Stream a file and stop reading after `max_bytes` (if given), so large files are
    never downloaded completely. Truncated content gets a marker appended.
    """
    encoded_path = urlquote(file_path, safe="")
    async with _gitlab_http.stream(
        "GET",
        f"/projects/{project_id}/repository/files/{encoded_path}/raw",
        params={"ref": branch},
    ) as resp:
        resp.raise_for_status()
        data = bytearray()
        truncated = False
        async for chunk in resp.aiter_bytes():
            data.extend(chunk)
            if max_bytes is not None and len(data) > max_bytes:
                del data[max_bytes:]
                truncated = True
                break

    content = data.decode("utf-8", errors="replace")
    if truncated:
        content += "\n... (file truncated)"
    return content


async def has_read_access(project_id: int, username: str) -> bool:
    """
    Check if the given user has at least read access rights to the GitLab project,
    based on the project's visibility, the user's ID as well as membership status.
    """
    # Get project visibility: "public", "internal", or "private"
    resp = await _gitlab_http.get(f"/projects/{project_id}")
    resp.raise_for_status()
    visibility = resp.json().get("visibility")
    # Public: everyone can read
    if visibility == "public":
        return True

    # Internal: all authenticated users can read
    resp = await _gitlab_http.get("/users", params={"username": username})
    resp.raise_for_status()
    users = resp.json()
    user_id = users[0].get("id") if users else None
    if user_id is None:
        return False

    if visibility == "internal":
        return True

    # Private: must have explicit membership
    resp = await _gitlab_http.get(f"/projects/{project_id}/members/all/{user_id}")
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return resp.json().get("access_level", 0) >= 10  # Guest and above can read


@alru_cache(maxsize=32, ttl=GITLAB_CACHE_SECONDS)
async def get_project_id(plugin: str, project: str) -> int | None:
    """Fetch the GitLab project ID for the given plugin name (cached briefly)."""
    key = f"{project}/plugins4freva/{plugin}"
    resp = await _gitlab_http.get(f"/projects/{urlquote(key, safe='')}")
    if resp.status_code == 404:
        project_id = None
    else:
        resp.raise_for_status()
        project_id = resp.json().get("id")
    return project_id


@alru_cache(maxsize=32, ttl=GITLAB_CACHE_SECONDS)
async def fetch_repo_tree_and_branch(project_id: int) -> tuple[list[str], str]:
    """
    Return the recursive file tree for a plugin repository as a tuple of
    file paths (strings) that match relevant extensions, plus the default branch.
    Results are cached briefly; the tuple keeps the cached value immutable.
    """
    items: list[dict] = []
    page: str | None = "1"
    while page:
        resp = await _gitlab_http.get(
            f"/projects/{project_id}/repository/tree",
            params={"recursive": "true", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        items.extend(resp.json())
        # GitLab sets an empty X-Next-Page header on the last page
        page = resp.headers.get("X-Next-Page") or None

    # filter to file paths with relevant extensions
    paths = [
        entry["path"]
        for entry in items
        if entry["type"] == "blob" and entry["path"].endswith(ALLOWED_FILE_EXTENSIONS)
    ]

    # Get the default branch of the repository (usually "levante" or "master")
    resp = await _gitlab_http.get(f"/projects/{project_id}")
    resp.raise_for_status()
    default_branch = resp.json().get("default_branch")
    return paths, default_branch


def file_paths_from_urls(
    file_urls: list[str], plugin: str, project: str, allowed_paths: set[str]
) -> tuple[list[str], str]:
    """
    Return unique repository paths named by valid GitLab file URLs,
    next to the branch they belong to.
    """
    expected_host = urlsplit(GITLAB_BASE_URL).hostname
    expected_prefix = f"/{project}/plugins4freva/{plugin}"

    selected_paths: list[str] = []
    branch = ""
    for fl_url in file_urls:
        try:
            parsed_url = urlsplit(fl_url)
            hostname = parsed_url.hostname
        except ValueError:
            continue
        url_path = unquote(parsed_url.path)
        if (
            hostname != expected_host
            or parsed_url.scheme not in {"http", "https"}
            or not url_path.startswith(expected_prefix)
        ):
            continue

        repo_url_path = url_path[len(expected_prefix) :]
        marker = "/-/blob/"
        marker_position = repo_url_path.find(marker)
        if marker_position < 0:
            marker = "/-/raw/"
            marker_position = repo_url_path.find(marker)
        if marker_position < 0:
            continue

        branch_file = repo_url_path[marker_position + len(marker) :]
        branch = branch_file.partition("/")[0]
        matches = [path for path in allowed_paths if branch_file.endswith(f"/{path}")]
        if matches:
            # Choose the longest matching path to avoid partial matches
            sel_path = max(matches, key=len)
            if sel_path not in selected_paths:
                selected_paths.append(sel_path)

    return selected_paths, branch


async def fetch_files(
    project_id: int, branch: str, selected_files: list[str], max_chars: int
) -> dict[str, str]:
    """
    Fetch the raw content of selected files in parallel and truncate them if applicable
    to fit within the character budget. Returns a mapping of file_path -> file_content.
    """
    collected: dict[str, str] = {}
    budget = max_chars

    results = await asyncio.gather(
        *[
            fetch_file_raw(project_id, file, branch, MAX_FILE_SIZE_BYTES)
            for file in selected_files
        ],
        return_exceptions=True,
    )

    for file, content in zip(selected_files, results):
        if budget <= 0:
            break
        if isinstance(content, BaseException):
            logger.info(
                "Skipping file %s due to error in fetching content: %s", file, content
            )
            continue
        if len(content) > budget:
            content = (
                content[:budget] + f"\n... (truncated: reached {max_chars} char limit)"
            )
        collected[file] = content
        budget -= len(content)

    return collected


# ── Other helpers ────────────────────────────────────────────────


def extract_method(source_code: str, method_name: str = "run_tool") -> str | None:
    """Parses a Python source string and returns the code of a specific method."""
    try:
        cleaned_code = textwrap.dedent(source_code.expandtabs(4))
        tree = ast.parse(cleaned_code)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                return ast.get_source_segment(cleaned_code, node)
    except SyntaxError as e:
        logger.warning("Could not parse source for '%s': %s", method_name, e)
    return None


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
    try:
        entry_file = sorted(matches, key=lambda p: (p.count("/"), p))[0]
        return entry_file
    except IndexError:
        return "No entry file found matching the pattern."


async def llm_file_selection(
    prompt: str, file_paths: list[str], num_files: int
) -> list[str]:
    """
    Ask the LLM for a JSON array of file paths. Keep and return only those that
    exist in `file_paths`. Falls back to using all available `file_paths`
    if the LLM response is invalid, empty or cannot be parsed.
    """
    try:
        raw_text = await call_llm(prompt)
        # Strip all markdown code fence markers
        raw_text = re.sub(r"```", "", raw_text)
        parsed_files = json.loads(raw_text.strip())
        if not isinstance(parsed_files, list) or not all(
            isinstance(path, str) for path in parsed_files
        ):
            raise ValueError("Expected a JSON array of file path strings")
        select_files: list[str] = parsed_files
    except Exception as e:
        logger.warning(
            "LLM file selection or parsing failed (%s); falling back to heuristic.", e
        )
    finally:
        # Fallback when selection failed, but preserve a valid empty array
        if "select_files" not in locals():
            select_files = file_paths

    # Return only paths that exist in repo tree and conform to exclusion pattern
    allowed_paths = set(file_paths)
    valid_paths = [
        p for p in select_files if p in allowed_paths and not EXCLUDE_PATTERN.search(p)
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
    Select the most relevant files from the repository based on the user query and
    the already fetched code that runs the plugin.
    """
    file_tree = "\n".join(file_paths)
    selection_prompt = (
        f"Task: You are analyzing the files from the '{plugin}' Freva plugin repository, given the user context and the fetched code (`run_tool` method) that runs the plugin. "
        "Rank and select files based on the following rules:\n\n"
        "- Always select the module that is called in the `run_tool` method; additionally select files that seem most relevant to the user's query.\n"
        "- For high level usage/configuration questions, prioritize config files, README and docs.\n"
        "- For questions about implementation logic, prioritize source code modules.\n"
        "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
        f"- Return ONLY a valid JSON array of at most {num_files} file path strings from the provided list. Output nothing but the JSON array.\n\n"
        f"=== User Query ===\n{user_query}\n=== END ===\n\n"
        f"=== Repository file list ===\n{file_tree}\n=== END ===\n\n"
        f"=== Fetched Code ===\n{fetched_code}\n=== END ===\n\n"
    )
    # Fallback: pick all files from the tree search
    return await llm_file_selection(selection_prompt, file_paths, num_files)


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
        f"Task: You are tracing the code base of the '{plugin}' Freva plugin, given the user query and the already fetched source code. "
        "Rank and select which of the remaining repository files should be read next based on the following rules:\n\n"
        "- Scan the fetched code for imports or script calls and map each to the matching file using Python module conventions "
        "(e.g., 'from foo.bar import baz' -> 'foo/bar.py').\n"
        "- Ignore imports from third-party libraries (e.g. numpy, xarray, evaluation_system etc.) and focus on the plugin's own source code.\n"
        "- Exclude tests, examples, generated files, and any '__init__.py'.\n"
        f"- Return ONLY a valid JSON array of at most {num_files} file path strings from the remaining list. If none match, return []. Output nothing but the JSON array.\n\n"
        f"=== User Query ===\n{user_query}\n=== END ===\n\n"
        f"=== Fetched Code ===\n{fetched_code}\n=== END ===\n\n"
        f"=== Remaining Repository Files ===\n{file_tree}\n=== END ===\n\n"
    )
    # Fallback: pick nothing to avoid fetching irrelevant code
    return await llm_file_selection(selection_prompt, file_paths, num_files)


async def collect_plugin_context(
    plugin: str,
    project: str,
    project_id: int,
    user_query: str,
    file_urls: list[str] | None = None,
) -> GenericToolResult:
    """
    Retrieve context from explicitly provided files, or use the plugin entry
    point when no files are provided, then resolve direct dependencies.
    Returns a GenericToolResult object containing the concatenated relevant source code files,
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
    file_paths, branch = await fetch_repo_tree_and_branch(project_id)
    if not file_paths:
        return GenericToolResult(
            error=f"repository is empty for plugin '{plugin}' in branch '{branch}'"
        )

    # ── Stage 1: find & fetch (provided) plugin entry ─────────────────────
    if file_urls:
        context_files, branch = file_paths_from_urls(
            file_urls, plugin, project, set(file_paths)
        )
        if not context_files:
            return GenericToolResult(
                error="None of the provided file URLs matched files in the plugin repository."
            )

        init_code = await fetch_files(
            project_id, branch, context_files, MAX_TOTAL_CODE_CHARS
        )
        budget = MAX_TOTAL_CODE_CHARS - sum(len(c) for c in init_code.values())
        _log_stage("Provided", context_files)
    else:
        entry_file = find_entry_file(file_paths)
        entry_source = await fetch_file_raw(project_id, entry_file, branch)
        exec_code = extract_method(entry_source, method_name="run_tool")
        entry_code = format_files(
            {entry_file: exec_code if exec_code is not None else entry_source}
        )

        # ── Stage 2: find & fetch useful files, based on entry file + user context ───────
        remaining = [path for path in file_paths if path != entry_file]
        init_files = await select_init_files(
            plugin, user_query, entry_code, remaining, MAX_FILES // 2
        )
        _log_stage("Initial", [entry_file] + init_files)
        init_code = {entry_file: entry_source}
        base_code = await fetch_files(
            project_id,
            branch,
            init_files,
            MAX_TOTAL_CODE_CHARS,
        )
        init_code.update(base_code)
        budget = MAX_TOTAL_CODE_CHARS - sum(len(c) for c in base_code.values())

    # ── Stage 2/3: resolve dependencies ───────────────────────────────────
    code_content = format_files(init_code)
    if budget <= 0:
        return GenericToolResult(result=header + code_content)

    remaining = [path for path in file_paths if path not in init_code]
    max_deps = max(0, MAX_FILES - len(init_code))
    dep_files = await select_dependency_files(
        plugin,
        user_query,
        code_content,
        remaining,
        max_deps,
    )
    _log_stage("Dependency", dep_files)
    deps_code = await fetch_files(project_id, branch, dep_files, budget)
    code_content += "\n\n### ── Dependency files ── ###\n\n" + format_files(deps_code)

    return GenericToolResult(result=header + code_content)


async def validate_plugin_call(
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

    user_name = _get_user()
    try:
        user_access = await has_read_access(project_id, user_name)
        if not user_access:
            logger.info(
                "User '%s' does NOT have read access to plugin '%s' in project '%s'.",
                user_name,
                plugin,
                project,
            )
            error_msg = (
                f"User access for {user_name} to plugin '{plugin}' denied! Possible solutions: "
                f"Get access by being added to DKRZ GitLab project '{project}', change your user permission for this project, or log in with appropriate credentials."
            )
            return False, error_msg
        logger.info(
            "Authorization layer passed: User has read access to plugin '%s' in project '%s'.",
            plugin,
            project,
        )
    except httpx.HTTPError as e:
        logger.warning("Error checking GitLab repo membership: %s", e)
        error_msg = "Plugin code search is currently unavailable (GitLab access error)."
        return False, error_msg

    return True, "Validation successful"


async def detect_plugin_project(user_query: str) -> tuple[str | None, str | None]:
    """
    Attempt to automatically detect the plugin and project names from the user's query
    by making a call to the LLM.

    Returns a tuple of (plugin, project):
    - plugin (str): Name of the repo or plugin (e.g. "leadtimeselektor") or None if not detected.
    - project (str): Name of the Freva instance (e.g. "kd1418" for "coming decade") or None if not detected.
    """
    file_path = Path(__file__).parent / "available_plugins.md"
    plugin_content = file_path.read_text(encoding="utf-8", newline="\n")
    avail_plugins = re.findall(r"\*\*([^*]+)\*\*", plugin_content, re.MULTILINE)

    # let LLM select plugin and project names from the available plugin summaries
    selection_prompt = (
        "Task: Select the semantically best matching Freva plugin & project name for the user query from the provided plugin summaries.\n"
        "Rules:\n"
        "- Select only plugin & project names that appear in the summaries.\n"
        "- Return exactly one plugin and its corresponding project.\n"
        "- Output must be exactly this format with no extra text: <plugin_name>,<project_name>\n\n"
        f"User query:\n{user_query}\n\n"
        f"Available plugin summaries:\n{plugin_content}"
    )
    raw_text = await call_llm(selection_prompt)

    # format to "<plugin_name>,<project_name>" output
    try:
        plugin_raw, project_raw = [
            x.strip().lower() for x in raw_text.split(",", maxsplit=1)
        ]
    except ValueError:
        return None, None

    avail_plugins = [p.lower() for p in avail_plugins]
    plugin = plugin_raw if plugin_raw in avail_plugins else None
    project = FREVA_PROJECT_NAMES.get(project_raw)
    logger.info(
        "Auto-detected plugin '%s' and project '%s' for examining code base.",
        plugin,
        project,
    )
    return plugin, project


@mcp.tool()
async def plugin_code_search(
    user_query: str, file_urls: list[str] | None = None
) -> GenericToolResult:
    """
    Fetch relevant source code and documentation for a Freva data analysis
    plugin. Use this tool for questions answerable from plugin source code,
    including its internal logic, usage, configuration, or requests to
    translate its code into Python examples. Ground responses in retrieved
    code.

    Use this tool when the user:
    1. asks about a plugin's logic, usage, or configuration, or requests a
            Python translation, by naming the plugin or linking its repository; or
    2. asks a complex climate or weather question involving a specialized topic below.
        Proactively call the tool to ground the answer in plugin logic:
        - Decadal prediction: lead-time selection or aggregation, skill scores
            against reanalysis or observations, cross-validation, bias correction,
            recalibration, or tercile statistics.
        - Climate and extreme indices from daily temperature or precipitation,
            including visualization.
        - Extreme-event impacts: crop productivity under compound events,
            heat-wave evaluation (HWMID), or intensity-duration-frequency analysis.
        - Precipitation indices, return periods, or sub-hourly disaggregation.
        - Spatio-temporal preprocessing, CMORization, or EOF/PCA analysis.
        - Regional climate analysis: urban heat-wave identification, matching
            climatically similar districts, or climate-model processing for
            impact studies.
        - Creating or structuring a new Freva plugin.

    Workflow Guidelines:
    1. Start with a high-level explanation of how the plugin works and how to
        use it. Mention specific details as needed.
    2. Provide an implementation only when requested. Translate the core logic
        into a lightweight Python snippet with a concise plan. Replace `cdo`
        with `xarray` and prioritize workflow correctness over retries,
        fallbacks, and logging.
    3. Call this tool again for follow-up questions not sufficiently covered by prior results.

    Args:
    -----
        user_query (str): What the user wants to know about or do with a dedicated Freva
            plugin regarding climate or weather data analysis.
        file_urls (list[str] | None): Optional GitLab file URLs to fetch in full
            before tracing their plugin-local dependencies.

    Returns:
    --------
        GenericToolResult: Relevant code context (with a header containing the plugin's repo URL),
        including directly imported dependency files; or an error message if the plugin
        is not found, user access is denied, or code retrieval fails.

    Examples:
    ---------
    - "How does the 'leadtimeselektor' plugin work from a high-level perspective?"
    - "How can I calculate climate prediction skill scores against observational data?"
    - "How can I assess the impact of extreme climate events on crop productivity?"
    """
    if not await gitlab_healthcheck():
        return GenericToolResult(
            error="GitLab server is not reachable or access token has become invalid."
        )

    plugin, project = await detect_plugin_project(user_query)
    if not plugin or not project:
        return GenericToolResult(
            error="No plugin or project could be matched from the user query."
        )

    # Validate the plugin call
    project_id = await get_project_id(plugin, project)
    result, message = await validate_plugin_call(plugin, project, project_id)
    if not result or project_id is None:
        return GenericToolResult(error=message)

    # Fetch the plugin code and return it with a header
    logger.info("Fetching source code for plugin '%s'", plugin)
    try:
        return await collect_plugin_context(
            plugin, project, project_id, user_query, file_urls
        )
    except Exception as e:
        logger.error("Failed to fetch plugin code for '%s': %s", plugin, e)
        return GenericToolResult(
            error=f"Failed to retrieve source code for plugin '{plugin}': {e}"
        )
