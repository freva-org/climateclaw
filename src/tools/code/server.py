import os
import asyncio
from contextvars import ContextVar
from pathlib import Path

from fastmcp import FastMCP

from src.core.logging_setup import configure_logging
from src.tools.header_gate import make_header_gate
from src.tools.active_requests import (
    ActiveRequest,
    RequestCancelled,
    current_ids,
    tracked_request,
)
from .code_execution import (
    get_sid_lock,
    execute_code,
    EXEC_TIMEOUT,
    cleanup_mcp_session,
    cancel_code_request,
)
from .kernels import shutdown_kernel, KERNEL_REGISTRY
from .helpers import sanitize_code, should_restart_after
from .safety_check import check_code_safety

SERVICE_NAME = os.getenv("HOSTNAME") or "code_server"

logger = configure_logging(__name__, named_log=SERVICE_NAME)

mcp = FastMCP("code-interpreter-server")

# ── App ───────────────────────────────────────────────────────────────────
# Per-request header context
CODE_INTERPRETER_USER_HDR = "username"
usr_ctx: ContextVar[str | None] = ContextVar("usr_ctx", default=None)
CODE_INTERPRETER_THID_HDR = "thread-id"
th_ctx: ContextVar[str | None] = ContextVar("th_ctx", default=None)

HOST = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("FREVAGPT_MCP_PORT", "8051"))
PATH = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path

# Configure Streamable HTTP transport
logger.info("Starting code-interpreter MCP server on %s:%s%s", HOST, PORT, PATH)


# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[usr_ctx, th_ctx],
    header_name_list=[CODE_INTERPRETER_USER_HDR, CODE_INTERPRETER_THID_HDR],
    logger=logger,
    mcp_path=PATH,
    on_session_close=cleanup_mcp_session,
    on_cancel_request=cancel_code_request,
)


def get_username():
    username = usr_ctx.get()
    if not username:
        raise ValueError
    else:
        return username
    
def get_threadid():
    thread_id = th_ctx.get()
    if not thread_id:
        raise ValueError
    else:
        return thread_id


def _run_code_request(
    username: str, 
    sid: str, 
    code: str, 
    working_dir: Path, 
    req: ActiveRequest
) -> dict:
    lock = get_sid_lock(sid)
    with lock:
        if req.cancelled_thread.is_set():
            raise RequestCancelled("Execution cancelled by client")

        sanitized_code = sanitize_code(code)
        out = execute_code(
            username=username,
            session_id=sid,
            code=sanitized_code,
            working_dir=working_dir,
            cancel_event=req.cancelled_thread,
            active_request=req,
        )

        if should_restart_after(sanitized_code):
            logger.warning("exit()/quit() detected; discarding kernel for sid=%s", sid)
            km = KERNEL_REGISTRY.get(sid)
            if km is not None:
                shutdown_kernel(km)
            KERNEL_REGISTRY.pop(sid, None)

        return out


@mcp.tool()
async def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    try:
        username = get_username()
    except Exception:
        logger.exception(
            f"Missing required header '{CODE_INTERPRETER_USER_HDR}'! "
        )
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": "Execution failed due to bad request: Missing header username.",
        }

    try:
        thread_id = get_threadid()
    except Exception:
        logger.exception(
            f"Missing required header '{CODE_INTERPRETER_THID_HDR}'! "
        )
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": "Execution failed due to bad request: Missing header username.",
        }
    
    thread_logger = configure_logging(__name__, thread_id=thread_id, user_id=username)

    session_id, request_id = current_ids()
    session_workdir = Path(thread_id)

    msg = f"Session id:{session_id}\nRequest id:{request_id}\nKernel execution timeout:{EXEC_TIMEOUT}"
    logger.debug(msg)
    thread_logger.debug(msg)

    stripped_code = code.replace("\n", "; ")
    logger.debug(f"Input code: {stripped_code}")
    thread_logger.debug(f"Input code: {stripped_code}")

    violation = check_code_safety(code)

    if violation:
        msg = (
            f"Code execution blocked by safety rule '{violation.rule_id}': "
            f"{violation.description} (matched: {violation.match!r})"
        )
        logger.warning(msg)
        thread_logger.warning(msg)
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }

    logger.info("Code block is safe to execute..")
    thread_logger.info("Code block is safe to execute..")

    try:
        async with tracked_request(session_id, request_id) as req:
            req.raise_if_cancelled()
            return await asyncio.to_thread(
                _run_code_request, username, session_id, code, session_workdir, req
            )
    # NOTE: a future refactor to make the whole pipeline async would be good (including the kernel management).
    # But this is a good start and allows the use of existing sync code execution logic with minimal changes.

    except RequestCancelled:
        msg = f"code_interpreter: execution cancelled for sid={session_id} request_id={request_id}"
        logger.info(msg)
        thread_logger.info(msg)
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": "Execution cancelled by client",
        }

    except InterruptedError as e:
        msg = f"code_interpreter: execution interrupted unexpectedly for sid={session_id} request_id={request_id}"
        logger.info(msg)
        thread_logger.info(msg)
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": f"Execution interrupted unexpectedly {e}",
        }

    except TimeoutError as e:
        msg = f"Execution failed: {e}"
        logger.exception(f"code_interpreter: execution timeout {msg}")
        thread_logger.exception(f"code_interpreter: execution timeout {msg}")
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }

    except Exception as e:
        msg = f"Execution failed: {type(e).__name__}: {e}"
        logger.exception(f"code_interpreter: execution error {e}")
        thread_logger.exception(f"code_interpreter: execution error {e}")
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }
