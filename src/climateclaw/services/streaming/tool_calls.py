from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import McpManager
from climateclaw.services.streaming.stream_variants import (
    OpenAIMessage,
    StreamVariant,
    SVCodeOutput,
    SVImage,
    SVToolOutput,
    SVUser,
    help_convert_sv_ccrm,
)

DEFAULT_LOGGER = configure_logging(__name__)

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


def accumulate_tool_calls(delta: dict[str, Any], agg: dict[str, Any]) -> None:
    choices = delta.get("choices") or []
    if not choices:
        return
    d = choices[0].get("delta") or {}
    tc_list = d.get("tool_calls") or []
    if not tc_list:
        return

    store: dict[int, dict[str, Any]] = agg.setdefault("by_index", {})
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


def finalize_tool_calls(agg: dict[str, Any]) -> list[dict[str, Any]]:
    store = agg.get("by_index") or {}
    out: list[dict[str, Any]] = []
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


def parse_tool_result(resp_txt: str, tool_name: str, call_id: str):
    result_json = json.loads(resp_txt)

    structured_content = result_json.get("structuredContent")
    if structured_content is not None:
        if tool_name == "code_interpreter":
            yield from parse_code_interpreter_result(structured_content, call_id)
        else:
            yield from parse_generic_tool_result(structured_content, tool_name, call_id)
    else:
        if result_json.get("error"):
            out = result_json.get("error")
        elif isinstance(result_json.get("content", {}), dict):
            out = result_json.get("content", {}).get("text", "Unknown response.")
        else:
            out = result_json.get("content", {})
        out_msg = f"{tool_name} error: {out}"

        if tool_name == "code_interpreter":
            toolout_v = SVCodeOutput(output=out_msg, id=call_id)
        else:
            toolout_v = SVToolOutput(output=out_msg, tool_name=tool_name, id=call_id)  # type: ignore[assignment]
        yield toolout_v
        tool_msg = help_convert_sv_ccrm([toolout_v])
        isError = True
        yield FinalSummary(
            var_block=[toolout_v], tool_messages=tool_msg, is_error=isError
        )


def parse_code_interpreter_result(result: dict, id: str):
    code_block: list[StreamVariant] = []
    code_msgs: list[OpenAIMessage] = []

    # Code output: structured dict of displayed data, image or error

    # Printed/displayed output + error message if exists
    out = (
        ""
        + (("\n" + result["stdout"]) if result["stdout"] else "")
        + (("\n" + result["result_repr"]) if result["result_repr"] else "")
    )
    out_error = (("\n" + result["stderr"]) if result["stderr"] else "") + (
        ("\n" + result["error"]) if result["error"] else ""
    )
    if out or out_error:
        codeout = out + out_error
    else:
        codeout = ""  # We must send something here, the model expects it.
    codeout_v = SVCodeOutput(output=codeout, id=id)
    yield codeout_v
    code_block.append(codeout_v)
    code_msgs.extend(help_convert_sv_ccrm([codeout_v]))

    # Image/html/json etc., rich output
    for i, r in enumerate(result.get("display_data", []) or []):
        if "image/png" in r.keys():
            base64_image = r["image/png"]
            image_id = id + f"_{i}"
            image_v = SVImage(b64=base64_image, id=image_id)
            yield image_v
            code_block.append(image_v)
            code_msgs.extend(
                help_convert_sv_ccrm(
                    [
                        SVUser(
                            text="Here is the image returned by the Code Interpreter."
                        ),
                        image_v,
                    ],
                    include_images=True,
                )
            )

        if "application/json" in r.keys():
            json_v = SVCodeOutput(output=r["application/json"], id=f"{id}:json")
            yield json_v
            code_block.append(json_v)
            code_msgs.extend(help_convert_sv_ccrm([json_v]))
    isError = True if out_error else False
    yield FinalSummary(var_block=code_block, tool_messages=code_msgs, is_error=isError)


def parse_generic_tool_result(result: dict, tool_name: str, id: str, logger=None):
    if result.get("result"):
        out = result.get("result", "")
    elif result.get("error"):
        out = result.get("error", "")
    else:
        out = "Unknown response."
    web_sv = SVToolOutput(output=out, tool_name=tool_name, id=id)
    web_msg = help_convert_sv_ccrm([web_sv])
    yield FinalSummary(var_block=[web_sv], tool_messages=web_msg, is_error=False)
