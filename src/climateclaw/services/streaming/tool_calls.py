from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import McpManager
from climateclaw.services.streaming.openai_helpers import (
    OpenAIMessage,
    help_convert_sv_ccrm,
)
from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVCodeOutput,
    SVImage,
    SVToolOutput,
    SVUser,
    normalize_code_output,
)

DEFAULT_LOGGER = configure_logging(__name__)
PROJECT_WEBSITE = os.environ.get("CLIMATECLAW_PROJECT_WEBSITE", "http://localhost:8000")

# ──────────────────────────────────────────────────────────────────────────────
# MCP tool runner
# ──────────────────────────────────────────────────────────────────────────────


async def run_tool_via_mcp(
    *,
    mcp: McpManager,
    tool_name: str,
    arguments_json: str,
    logger=None,
) -> str:
    log = logger or DEFAULT_LOGGER
    try:
        args = json.loads(arguments_json or "{}")
    except Exception:
        args = {"_raw": arguments_json}

    server_name = await mcp.get_server_from_tool(tool_name)
    if server_name is None:
        log.error(f"No MCP server found for tool={tool_name}")
        raise RuntimeError(f"No MCP server found for tool={tool_name}")

    log.info(f"Executing tool call:\nname : {tool_name}   arguments : {args}")
    res = await mcp.call_tool(
        server_name,
        name=tool_name,
        arguments=args,
    )

    return json.dumps(res)


# ──────────────────────────────────────────────────────────────────────────────
# Tool-call accumulation helpers (OpenAI-style deltas)
# ──────────────────────────────────────────────────────────────────────────────


def accumulate_tool_calls(delta: Dict[str, Any], agg: Dict[str, Any]) -> None:
    choices = delta.get("choices") or []
    if not choices:
        return
    d = choices[0].get("delta") or {}
    tc_list = d.get("tool_calls") or []
    if not tc_list:
        return

    store: Dict[int, Dict[str, Any]] = agg.setdefault("by_index", {})
    for item in tc_list:
        idx = item.get("index")
        if idx is None:
            continue
        entry = store.setdefault(
            idx, {"type": "function", "function": {"name": "", "arguments": ""}}
        )
        if item.get("id"):
            entry["id"] = item["id"]
        f = item.get("function") or {}
        if f.get("name"):
            entry["function"]["name"] = f["name"]
        if f.get("arguments"):
            entry["function"]["arguments"] = (
                entry["function"].get("arguments", "") + f["arguments"]
            )


def finalize_tool_calls(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = agg.get("by_index") or {}
    out: List[Dict[str, Any]] = []
    for idx in sorted(store.keys()):
        tc = store[idx]
        fn = tc.get("function") or {}
        tc.setdefault("type", "function")
        tc["function"] = {
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
        }
        out.append(tc)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Tool result parsers
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class FinalSummary:
    var_block: list[StreamVariant]
    tool_messages: list[OpenAIMessage]
    is_error: bool


def parse_tool_result(
    resp_txt: str, tool_name: str, call_id: str, thread_id: str, logger=None
):
    log = logger or DEFAULT_LOGGER
    result_json = json.loads(resp_txt)
    toolout_v: StreamVariant

    structured_content = result_json.get("structuredContent")
    if structured_content is not None:
        if tool_name == "code_interpreter":
            yield from parse_code_interpreter_result(
                structured_content, call_id, thread_id, logger=log
            )
        else:
            yield from parse_generic_tool_result(
                structured_content, tool_name, call_id, logger=log
            )
    else:
        if result_json.get("error"):
            out = result_json.get("error")
        elif isinstance(result_json.get("content", {}), Dict):
            out = result_json.get("content", {}).get("text", "Unknown response.")
        else:
            out = result_json.get("content", {})
        out_msg = f"{tool_name} error: {out}"

        if tool_name == "code_interpreter":
            toolout_v = SVCodeOutput(content=normalize_code_output(out_msg), id=call_id)
        else:
            toolout_v = SVToolOutput(content=out_msg, tool_name=tool_name, id=call_id)
        yield toolout_v
        tool_msg = help_convert_sv_ccrm([toolout_v])
        isError = True
        yield FinalSummary(
            var_block=[toolout_v], tool_messages=tool_msg, is_error=isError
        )


def parse_code_interpreter_result(result: Dict, id: str, thread_id: str, logger=None):
    code_block: List[StreamVariant] = []
    code_msgs: List[OpenAIMessage] = []

    # Code output: structured dict of displayed data, image or error

    # Check if any file was created
    created_files = result.get("created_files", [])
    for file in created_files:
        f_name = file.get("path")

        if not f_name:
            continue

        file["preview_url"] = (
            f"{PROJECT_WEBSITE}/static/preview/climateclaw/{thread_id}/{f_name}"
        )

    result["created_files"] = created_files

    codeout_v = SVCodeOutput(content=normalize_code_output(result), id=id)
    code_msgs.extend(help_convert_sv_ccrm([codeout_v]))
    code_block.append(codeout_v)
    yield codeout_v

    # Get number of images in "display_data" - contains rich output, image/html/json
    num_display_data_with_png = sum(
        1
        for item in result.get("display_data", [])
        if isinstance(item, dict) and "image/png" in item
    )

    num_saved_images = sum(
        1
        for file in created_files
        if isinstance(file, dict) and file.get("mime_type") == "image/png"
    )

    # If there are more images streamed than saved, we stream them all to client and model
    if num_display_data_with_png > num_saved_images:
        for i, r in enumerate(result.get("display_data", []) or []):
            if "image/png" in r.keys():
                base64_image = r["image/png"]
                image_id = id + f"_{i}"
                image_v = SVImage(content=base64_image, id=image_id)
                yield image_v
                code_block.append(image_v)
                code_msgs.extend(
                    help_convert_sv_ccrm(
                        [
                            SVUser(
                                content="Here is the image returned by the Code Interpreter."
                            ),
                            image_v,
                        ],
                        include_images=True,
                    )
                )

    isError = True if result.get("error", "") or result.get("stderr", "") else False
    yield FinalSummary(var_block=code_block, tool_messages=code_msgs, is_error=isError)


def parse_generic_tool_result(result: Dict, tool_name: str, id: str, logger=None):
    if result.get("result"):
        out = result.get("result")
    elif result.get("error"):
        out = result.get("error")
    else:
        out = "Unknown response."
    web_sv = SVToolOutput(content=out, tool_name=tool_name, id=id)  # type: ignore[arg-type]
    web_msg = help_convert_sv_ccrm([web_sv])
    yield FinalSummary(var_block=[web_sv], tool_messages=web_msg, is_error=False)
