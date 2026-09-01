from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from climateclaw.core.available_chatbots import model_is_ollama, model_supports_images
from climateclaw.core.heartbeat import heartbeat_content
from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import Authenticator, ThreadStorage
from climateclaw.services.streaming.active_conversations import (
    ConversationState,
    add_to_conversation,
    get_conv_mcpmanager,
    get_conv_messages,
    get_conversation_state,
    get_replay_task,
    initialize_conversation,
    register_tool_task,
    unregister_tool_task,
)
from climateclaw.services.streaming.litellm_client import acomplete, first_text
from climateclaw.services.streaming.openai_helpers import (
    OpenAIMessage,
    help_convert_sv_ccrm,
)
from climateclaw.services.streaming.replay_gate import ReplayGate
from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVAssistant,
    SVCode,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVToolCall,
    SVUser,
    from_json_to_sv,
)
from climateclaw.services.streaming.tool_calls import (
    FinalSummary,
    InvalidToolArguments,
    accumulate_tool_calls,
    code_variant_content,
    finalize_tool_calls,
    get_tool_input_schema,
    normalize_tool_arguments,
    parse_tool_result,
    run_tool_via_mcp,
)

DEFAULT_LOGGER = configure_logging(__name__)
HEARTBEAT_INTERVAL_SECONDS = 1


@dataclass
class StreamState:
    user_invoked: bool = True
    tool_call: dict[str, Any] | None = None
    finished: bool = False
    replay_gate: ReplayGate | None = None


async def _yield_heartbeats_until(
    task: asyncio.Task[Any],
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[SVServerHint]:
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except TimeoutError:
            yield await heartbeat_content()


# ──────────────────────────────────────────────────────────────────────────────
# Streaming with tools
# ──────────────────────────────────────────────────────────────────────────────


async def stream_with_tools(
    *,
    model: str,
    thread_id: str,
    messages: list[dict[str, Any]],  # system_prompt
    acomplete_func=acomplete,
    stream_state: StreamState,
    storage: ThreadStorage,
    store_thread: bool = True,
    logger=None,
) -> AsyncIterator[StreamVariant]:
    log = logger or DEFAULT_LOGGER

    # Append the conversation history to system prompt
    conv_sv = await get_conv_messages(thread_id)
    msg_hist = help_convert_sv_ccrm(
        conv_sv,  # type: ignore[arg-type]
        include_images=model_supports_images(model),
        include_meta=False,
    )
    messages.extend(msg_hist)  # type: ignore[arg-type]

    # Get MCPManager of the conversation
    mcp = await get_conv_mcpmanager(thread_id)

    if stream_state.replay_gate is None:
        stream_state.replay_gate = ReplayGate(await get_replay_task(thread_id))

    if hint := stream_state.replay_gate.start_hint():
        yield hint

    # 1) First request
    tool_agg: dict[str, Any] = {}
    tools = (
        await mcp.available_tools() if mcp and hasattr(mcp, "available_tools") else []
    )

    if tools:
        resp_task = asyncio.create_task(
            acomplete_func(
                model=model,
                messages=messages,
                stream=True,
                tools=tools,
                tool_choice="auto",
            )
        )
    else:
        resp_task = asyncio.create_task(
            acomplete_func(model=model, messages=messages, stream=True)
        )

    try:
        async for hb in _yield_heartbeats_until(resp_task):
            yield hb
        resp = await resp_task
    except asyncio.CancelledError:
        resp_task.cancel()
        await asyncio.gather(resp_task, return_exceptions=True)
        raise

    accumulated_asst_text: list[str] = []

    if hasattr(resp, "__aiter__"):
        call_id = ""
        stream_iter = resp.__aiter__()
        while True:
            chunk_task = asyncio.create_task(stream_iter.__anext__())

            try:
                async for hb in _yield_heartbeats_until(chunk_task):
                    yield hb
                chunk = await chunk_task
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                chunk_task.cancel()
                await asyncio.gather(chunk_task, return_exceptions=True)
                raise

            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}

            # assistant text
            piece = delta.get("content") or ""
            if piece:
                accumulated_asst_text.append(piece)
                yield SVAssistant(content=piece)

            # tool call: stream code chunks live and accumulate deltas
            tc_list = delta.get("tool_calls") or []
            if tc_list:
                accumulate_tool_calls({"choices": [{"delta": delta}]}, tool_agg)
                by_index = tool_agg.get("by_index") or {}

                for tc in tc_list:
                    idx = tc.get("index")
                    aggregated_tc = by_index.get(idx, {})

                    fn = tc.get("function") or {}
                    aggregated_fn = aggregated_tc.get("function") or {}

                    tool_name = fn.get("name") or aggregated_fn.get("name")
                    call_id = tc.get("id") or aggregated_tc.get("id") or call_id
                    args_chunk = fn.get("arguments", "")

                    if (
                        args_chunk
                        and tool_name == "code_interpreter"
                        and not model_is_ollama(model)
                    ):
                        # stream arguments chunk immediately
                        yield SVCode(content=args_chunk, id=call_id)

            #  end-of-message
            if choice.get("finish_reason"):
                break
    else:
        full_txt = first_text(resp) or ""
        for p in re.findall(r"\S+\s*", full_txt):
            if p:
                accumulated_asst_text.append(full_txt)
                yield SVAssistant(content=full_txt)

    # 2) Any tool calls?
    tool_calls = finalize_tool_calls(tool_agg)
    log.debug(f"Finalized tool calls: {tool_calls}")

    if accumulated_asst_text:
        asst_v = SVAssistant(content="".join(accumulated_asst_text))
        await add_to_conversation(
            thread_id, [asst_v], storage=storage, store_thread=store_thread
        )

    # If no tool calls, wrap up everything and return
    if not tool_calls:
        if hint := await stream_state.replay_gate.wait_done_hint():
            yield hint

        end_v = SVStreamEnd(content="Stream ended.")
        yield end_v
        stream_state.finished = True
        return

    # 3) Run tools
    id = ""
    for tc in tool_calls:
        function = tc.get("function") or {}
        name = function.get("name", "")
        id = tc.get("id", id)
        raw_args_txt = function.get("arguments", "")

        log.info(
            f"Received tool execution: name={name} raw_arguments={raw_args_txt}",
        )

        normalization_error: str | None = None

        try:
            input_schema = await get_tool_input_schema(mcp, name) if mcp else None

            if input_schema is None:
                raise InvalidToolArguments(f"No input schema found for tool {name}.")

            normalized = normalize_tool_arguments(
                raw_arguments=raw_args_txt,
                input_schema=input_schema,
            )

            args_txt = json.dumps(normalized.arguments)

            if normalized.was_unwrapped:
                log.warning(
                    "Normalized one-level tool argument wrapper: "
                    "tool=%r id=%r wrapper=%r raw_arguments=%r",
                    name,
                    id,
                    normalized.wrapper_key,
                    raw_args_txt,
                )

            # Replace the malformed arguments in the actual assistant
            # tool-call message sent back to the model.
            tc["function"] = {
                **function,
                "arguments": args_txt,
            }

        except InvalidToolArguments as exc:
            args_txt = raw_args_txt

            if name == "code_interpreter":
                normalization_error = (
                    "Invalid code_interpreter arguments. Call the tool again with exactly "
                    "one top-level field: {'code': '<complete Python script including all imports>'}"
                    "Do not use import_statements, imports, args, arguments, argument, or tool."
                )
            else:
                normalization_error = (
                    f"Invalid arguments for tool {name}: {exc} "
                    "Retry the tool call using its declared input schema exactly."
                )

            log.warning(
                "Rejected malformed tool call: tool=%r id=%r arguments=%r error=%s",
                name,
                id,
                raw_args_txt,
                exc,
            )

        # Append assistant tool-call message
        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})

        # Store valid, normalized arguments in MongoDB and stream them to the frontend.
        # Otherwise some client may break.
        if name == "code_interpreter":
            if hint := await stream_state.replay_gate.wait_done_hint():
                yield hint

            # accumulated code text to be appended to thread
            tool_v = SVCode(
                content=code_variant_content(
                    raw_arguments=raw_args_txt,
                    normalized_arguments=(
                        args_txt if normalization_error is None else None
                    ),
                ),
                id=call_id,
            )
            if model_is_ollama(model):
                yield tool_v
        else:
            tool_v = SVToolCall(
                content=(args_txt if normalization_error is None else raw_args_txt),
                id=call_id,
                tool_name=name,
            )  # type: ignore[assignment]
            # code is already streamed, we stream the other tool calls here too
            yield tool_v

        await add_to_conversation(
            thread_id, [tool_v], storage=storage, store_thread=store_thread
        )

        async def run_with_heartbeat():
            """Run the tool while sending heartbeats during quiet periods."""
            tool_task = asyncio.create_task(
                run_tool_via_mcp(
                    mcp=mcp,
                    tool_name=name,
                    arguments_json=args_txt,
                    logger=log,
                )
            )

            await register_tool_task(thread_id, tool_task)

            try:
                async for hb in _yield_heartbeats_until(tool_task):
                    yield hb

                # When done, return the final result text
                result_text = await tool_task
                yield result_text

            except asyncio.CancelledError as e:
                conv_state = await get_conversation_state(thread_id)
                task = asyncio.current_task()

                log.error(
                    "RUN_WITH_HEARTBEAT CANCELLED: "
                    "thread=%s state=%s tool=%s "
                    "task=%r cancelling=%s "
                    "error=%r args=%r "
                    "tool_task=%r tool_task_done=%s tool_task_cancelled=%s",
                    thread_id,
                    conv_state,
                    name,
                    task,
                    task.cancelling() if task else None,
                    e,
                    e.args,
                    tool_task,
                    tool_task.done(),
                    tool_task.cancelled(),
                )

                if conv_state == ConversationState.STOPPING:
                    log.warning(
                        "Tool task cancelled; interrupting MCP execution for thread=%s",
                        thread_id,
                    )

                    result_text = json.dumps(
                        {
                            "structuredContent": {
                                "error": "Tool task cancelled upon user request."
                            }
                        }
                    )
                    yield result_text

                else:
                    log.exception(
                        "Tool task cancelled unexpectedly; thread=%s state=%s tool=%s",
                        thread_id,
                        conv_state,
                        name,
                    )

                    tool_task.cancel()
                    await asyncio.gather(tool_task, return_exceptions=True)
                    raise

            except Exception:
                tool_task.cancel()
                await asyncio.gather(tool_task, return_exceptions=True)
                raise

            finally:
                # Ensure the task is removed from the registry when it finishes
                await unregister_tool_task(thread_id, tool_task)

        result_text: str | None = None
        heartbeats_v: list[StreamVariant] = []

        if normalization_error is not None:
            # Do not call MCP. Feed a normal tool error back to the model.
            result_text = json.dumps(
                {
                    "error": normalization_error,
                }
            )
        else:
            try:
                async for item in run_with_heartbeat():
                    if isinstance(item, SVServerHint):
                        yield item  # Stream heartbeat ServerHint variants
                        heartbeats_v.append(item)
                    elif isinstance(item, str):
                        # The function returns the final tool result as last value
                        result_text = item

            except Exception as e:
                log.exception("Tool {name} failed")
                result_text = json.dumps({"error": str(e)})

        if result_text is None:
            result_text = json.dumps(
                {
                    "error": f"Tool {name} returned no result.",
                }
            )

        tool_out_v: list[StreamVariant] = []
        tool_msgs: list[OpenAIMessage] = []
        # Parsing tool call output as StreamVariants and messages to model
        for r in parse_tool_result(
            result_text,
            tool_name=name,
            call_id=id,
            include_images=model_supports_images(model),
        ):
            if isinstance(r, FinalSummary):
                (
                    tool_out_v,
                    tool_msgs,
                ) = r.var_block, r.tool_messages
                break
            else:
                yield r  # Streaming the result to endpoint

        await add_to_conversation(
            thread_id, tool_out_v, storage=storage, store_thread=store_thread
        )

        if hint := stream_state.replay_gate.done_hint_if_ready():
            yield hint

        if tool_msgs:
            messages.extend(tool_msgs)  # type: ignore[arg-type]

        if await get_conversation_state(thread_id) == ConversationState.STOPPING:
            return


# ──────────────────────────────────────────────────────────────────────────────
# High-level orchestrator (storage-agnostic)
# ──────────────────────────────────────────────────────────────────────────────


async def run_stream(
    *,
    model: str,
    thread_id: str,
    user_input: str,
    system_prompt: list[dict[str, Any]],
    storage: ThreadStorage,
    store_thread: bool = True,
    logger=None,
) -> AsyncGenerator[StreamVariant, None]:
    """
    Orchestrate a single turn, yielding StreamVariant objects.
    """
    log = logger or DEFAULT_LOGGER

    # Append user content
    user_v = SVUser(content=user_input or "", model=model)
    await add_to_conversation(
        thread_id, [user_v], storage=storage, store_thread=store_thread
    )

    stream_state = StreamState()

    # Stream model/tool output
    while not stream_state.finished:
        conv_state = await get_conversation_state(thread_id)
        if conv_state != ConversationState.STREAMING:
            break
        try:
            async for piece in stream_with_tools(
                thread_id=thread_id,
                messages=system_prompt,
                model=model,
                acomplete_func=acomplete,
                stream_state=stream_state,
                storage=storage,
                store_thread=store_thread,
                logger=log,
            ):
                yield piece

        except asyncio.CancelledError as e:
            conv_state = await get_conversation_state(thread_id)

            task = asyncio.current_task()

            log.error(
                "RUN_STREAM CANCELLED: "
                "thread=%s state=%s "
                "task=%r cancelling=%s "
                "error=%r args=%r",
                thread_id,
                conv_state,
                task,
                task.cancelling() if task else None,
                e,
                e.args,
            )

            if conv_state == ConversationState.STOPPING:
                log.info(
                    "Stream cancelled after client stop request; thread=%s", thread_id
                )
            else:
                log.exception(
                    "Stream cancelled unexpectedly; thread=%s state=%s",
                    thread_id,
                    conv_state,
                )
            stream_state.finished = True
        except Exception as e:
            log.exception("Stream error: %s", e)
            err_v = SVServerError(content=str(e))
            end_v = SVStreamEnd(content="Stream ended with an error.")
            await add_to_conversation(
                thread_id, [err_v], storage=storage, store_thread=store_thread
            )
            stream_state.finished = True
            yield err_v
            yield end_v


async def prepare_for_stream(
    thread_id: str,
    user_id: str,
    Auth: Authenticator,
    Storage: ThreadStorage | None = None,
    read_history: bool | None = False,
    logger=None,
) -> None:
    """
    Preparations for the streaming, read history (if needed), add to Registry and
    set conversation state to "streaming".
    Returns the conversation history as StreamVariants if `read_history` is True.
    """
    log = logger or DEFAULT_LOGGER
    messages: list[StreamVariant] = []
    if read_history and Storage:
        messages = await get_conversation_history(thread_id, Storage)

    # Check if the conversation already exists in registry
    # If not initialize it, and add the first messages
    await initialize_conversation(
        thread_id, user_id, messages=messages, auth=Auth, logger=log
    )

    if messages:
        log.info("Conversation history loaded with %d messages.", len(messages))
    return None


async def get_conversation_history(
    thread_id: str,
    Storage: ThreadStorage,
) -> list[StreamVariant]:
    # Build messages for ongoing conversation
    prior_json: list[dict] = await Storage.read_thread(thread_id)
    prior_sv: list[StreamVariant] = [from_json_to_sv(item) for item in prior_json]
    return prior_sv


__all__ = ["run_stream", "stream_with_tools"]
