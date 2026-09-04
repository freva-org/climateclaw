import asyncio
import os
from contextvars import ContextVar

from fastmcp import FastMCP

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.streaming.litellm_client import acomplete, first_text
from climateclaw.tools.active_requests import (
    ACTIVE_REQUESTS,
    RequestCancelled,
    current_ids,
    tracked_request,
)
from climateclaw.tools.header_gate import make_header_gate
from climateclaw.tools.reference.registry import (
    get_reference,
    reference_catalog,
)

logger = configure_logging(__name__)

mcp = FastMCP("reference-server")

# ── Config ───────────────────────────────────────────────────────────────────

HOST = os.getenv("CLIMATECLAW_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("CLIMATECLAW_MCP_PORT", "8053"))
PATH = os.getenv("CLIMATECLAW_MCP_PATH", "/mcp")  # standard path


# ─── App ────────────────────────────────────────────────────────────────────
# Per-request header context
MODEL_HDR = "climateclaw-model"
model_ctx: ContextVar[str | None] = ContextVar("climateclaw-model", default=None)

logger.info("Starting Reference MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[model_ctx],
    header_name_list=[MODEL_HDR],
    logger=logger,
    mcp_path=PATH,
    on_cancel_request=ACTIVE_REQUESTS.cancel,
)


def get_model():
    model = model_ctx.get()
    if not model:
        logger.error(f"Missing required header '{MODEL_HDR}'! ")
        raise RuntimeError("Missing ClimateClaw model header for consult_references")
    else:
        return model


# ─── Tool ───────────────────────────────────────────────────────────────────

CONSULT_REFERENCES_DESCRIPTION = f"""
Consult one or more authoritative reference documents when they are relevant
to the user's question.

Choose the references yourself based on the user's question and the descriptions
below. Only select references whose contents are useful for answering the
question.

The `references` argument contains reference IDs, not URLs.

Available references:

{reference_catalog()}
""".strip()


@mcp.tool(description=CONSULT_REFERENCES_DESCRIPTION)
async def consult_references(
    question: str,
    references: list[str],
) -> dict[str, str]:
    """
    Consult selected reference documents using the same model as the main
    ClimateClaw conversation.
    """
    sid, rid = current_ids()
    model = get_model()

    if not references:
        logger.error("Missing tool call parameter: references")
        return {
            "result": "",
            "error": "Missing tool call parameter: references. At least one reference must be selected.",
        }

    selected = [get_reference(reference_id) for reference_id in references]
    citations = [reference.url for reference in selected]

    logger.info(
        "Consulting references. model=%s references=%s question=%r",
        model,
        [reference.id for reference in selected],
        question,
    )

    try:
        async with tracked_request(sid, rid) as req:
            req.raise_if_cancelled()

            content: list[dict[str, str]] = []

            # We use native file inputs, without any PDF parsing or text extraction.
            for reference in selected:
                content.append(
                    {
                        "type": "input_file",
                        "file_url": reference.url,
                    }
                )

            content.append(
                {
                    "type": "input_text",
                    "text": question,
                }
            )

            call = asyncio.create_task(
                acomplete(
                    model=model,
                    endpoint="v1/responses",
                    messages=[
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                    stream=False,
                )
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
                    raise RequestCancelled("Reference consultation cancelled by client")

                response = call.result()

            finally:
                for task in (call, waiter):
                    if not task.done():
                        task.cancel()

            req.raise_if_cancelled()

            result = first_text(response) + f"\n Citations: {citations}"

            return {
                "result": result,
                "error": "",
            }

    except asyncio.CancelledError:
        raise

    except RequestCancelled:
        logger.info(
            "Reference consultation cancelled. sid=%s rid=%s",
            sid,
            rid,
        )
        return {
            "result": "",
            "error": "Request cancelled by client.",
        }

    except Exception as exc:
        logger.warning(
            "Reference consultation failed: %s",
            exc,
        )
        raise RuntimeError(f"Reference consultation failed: {exc}") from exc
