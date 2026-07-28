from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

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


class InvalidToolArguments(ValueError):
    """Raised when tool arguments do not conform to the tool's JSON schema."""


@dataclass(frozen=True)
class NormalizedToolArguments:
    arguments: dict[str, Any]
    was_unwrapped: bool = False
    wrapper_key: str | None = None


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

    server_name = mcp.get_server_from_tool(tool_name)

    log.info(f"Executing tool call:\nname : {tool_name}   arguments : {args}")
    # Run the blocking MCP call in a thread so cancellation of the coroutine
    # doesn’t block the event loop.
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: mcp.call_tool(
            server_name,
            name=tool_name,
            arguments=args,
        ),
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


def normalize_tool_arguments(
    raw_arguments: str,
    input_schema: dict[str, Any],
) -> NormalizedToolArguments:
    """
    Validate model-generated tool arguments.

    If the direct arguments do not satisfy the schema, allow exactly one
    arbitrary wrapper around an object that does satisfy the schema.

    Examples:

        {"code": "..."}
        -> accepted unchanged

        {"args": {"code": "..."}}
        -> normalized if {"code": "..."} satisfies that tool's schema

        {"anything": "...", "code": "..."}
        -> rejected
    """
    arguments = _parse_tool_arguments(raw_arguments)

    if not input_schema:
        raise InvalidToolArguments("No input schema is available for this tool.")

    validator = Draft202012Validator(dict(input_schema))

    direct_error = _first_validation_error(validator, arguments)
    if direct_error is None:
        return NormalizedToolArguments(arguments=arguments)

    # Only unwrap an unambiguous, one-property object:
    # {"any_wrapper": {...correct arguments...}}
    if len(arguments) == 1:
        wrapper_key, wrapped_value = next(iter(arguments.items()))

        if isinstance(wrapped_value, dict):
            wrapped_error = _first_validation_error(
                validator,
                wrapped_value,
            )

            if wrapped_error is None:
                return NormalizedToolArguments(
                    arguments=wrapped_value,
                    was_unwrapped=True,
                    wrapper_key=wrapper_key,
                )

    raise InvalidToolArguments(
        "Tool arguments do not match the declared input schema. "
        f"Received: {arguments!r}. "
        f"Validation error: {_format_validation_error(direct_error)}"
    )


def _parse_tool_arguments(
    raw_arguments: str,
) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise InvalidToolArguments(
                f"Tool arguments are not a valid JSON string: {exc}"
            ) from exc
    else:
        raise InvalidToolArguments(
            f"Tool arguments must be a JSON string, not {type(raw_arguments).__name__}."
        )

    if not isinstance(parsed, dict):
        raise InvalidToolArguments(
            f"Tool arguments must decode to a JSON object, not {type(parsed).__name__}."
        )

    return parsed


def _first_validation_error(
    validator: Draft202012Validator,
    arguments: dict[str, Any],
) -> ValidationError | None:
    return next(iter(validator.iter_errors(arguments)), None)


def _format_validation_error(error: ValidationError) -> str:
    if error.absolute_path:
        path = ".".join(str(part) for part in error.absolute_path)
        return f"{path}: {error.message}"

    return error.message


def get_tool_input_schema(
    mcp: McpManager,
    tool_name: str,
) -> dict[str, Any] | None:
    """
    Find a tool's input schema from the OpenAI-style tool definitions
    cached by McpManager.
    """
    for tool in mcp.available_tools():
        if not isinstance(tool, dict):
            continue

        function = tool.get("function")
        if not isinstance(function, dict):
            continue

        if function.get("name") != tool_name:
            continue

        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            return parameters

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Tool result parsers
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class FinalSummary:
    var_block: list[StreamVariant]
    tool_messages: list[OpenAIMessage]
    is_error: bool


def parse_tool_result(
    resp_txt: str, tool_name: str, call_id: str, include_images: bool
):
    result_json = json.loads(resp_txt)

    structured_content = result_json.get("structuredContent")
    if structured_content is not None:
        if tool_name == "code_interpreter":
            yield from parse_code_interpreter_result(
                structured_content, call_id, include_images
            )
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


def parse_code_interpreter_result(result: dict, id: str, include_images: bool):
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
        codeout = "Execution completed successfully."  # We must send something here, the model expects it.
    codeout_v = SVCodeOutput(output=codeout, id=id)
    yield codeout_v
    code_block.append(codeout_v)
    code_msgs.extend(help_convert_sv_ccrm([codeout_v]))

    # Image/html/json etc., rich output
    for i, r in enumerate(result.get("display_data", []) or []):
        if "image/png" in r.keys() and include_images:
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


def parse_generic_tool_result(result: dict, tool_name: str, id: str):
    web_sv = SVToolOutput(output=result.get("result", ""), tool_name=tool_name, id=id)
    web_msg = help_convert_sv_ccrm([web_sv])
    yield FinalSummary(var_block=[web_sv], tool_messages=web_msg, is_error=False)
