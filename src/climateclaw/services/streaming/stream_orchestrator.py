from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from climateclaw.core.available_chatbots import model_supports_images
from climateclaw.core.heartbeat import heartbeat_content
from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import Authenticator, ThreadStorage
from climateclaw.services.streaming.active_conversations import (
    ConversationState,
    add_to_conversation,
    get_conv_mcpmanager,
    get_conv_messages,
    get_conversation_state,
    initialize_conversation,
    register_tool_task,
    unregister_tool_task,
)
from climateclaw.services.streaming.litellm_client import acomplete, first_text
from climateclaw.services.streaming.openai_helpers import (
    OpenAIMessage,
    help_convert_sv_ccrm,
)
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
    accumulate_tool_calls,
    finalize_tool_calls,
    parse_tool_result,
    run_tool_via_mcp,
)

DEFAULT_LOGGER = configure_logging(__name__)


@dataclass
class StreamState:
    user_invoked: bool = True
    tool_call: dict[str, Any] | None = None
    finished: bool = False


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

    # 1) First request
    tool_agg: dict[str, Any] = {}
    tools = await mcp.openai_tools() if mcp and hasattr(mcp, "openai_tools") else []

    if tools:
        resp = await acomplete_func(
            model=model, messages=messages, stream=True, tools=tools, tool_choice="auto"
        )
    else:
        resp = await acomplete_func(model=model, messages=messages, stream=True)

    accumulated_asst_text: list[str] = []

    if hasattr(resp, "__aiter__"):
        call_id = ""
        async for chunk in resp:
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
                tool_name = (
                    tool_agg.get("by_index", [])[0].get("function").get("name")
                    if tool_agg
                    else None
                )
                for tc in tc_list:
                    fn = tc.get("function") or {}
                    call_id = tc.get("id", call_id)
                    args_chunk = fn.get("arguments", "")
                    if args_chunk and tool_name == "code_interpreter":
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

    if accumulated_asst_text:
        asst_v = SVAssistant(content="".join(accumulated_asst_text))
        await add_to_conversation(
            thread_id, [asst_v], storage=storage, store_thread=store_thread
        )

    # If no tool calls, wrap up everything and return
    if not tool_calls:
        end_v = SVStreamEnd(content="Stream ended.")
        yield end_v
        stream_state.finished = True
        return

    # 3) Run tools
    id = ""
    for tc in tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        name = (tc.get("function") or {}).get("name", "")
        id = tc.get("id", id)
        args_txt = (tc.get("function") or {}).get("arguments", "")

        if name == "code_interpreter":
            # accumulated code text to be appended to thread
            tool_v = SVCode(content=args_txt, id=id)
        else:
            tool_v = SVToolCall(content=args_txt, id=id, tool_name=name)  # type: ignore[assignment]
            # code is already streamed, we stream the other tool calls here too
            yield tool_v

        await add_to_conversation(
            thread_id, [tool_v], storage=storage, store_thread=store_thread
        )

        async def run_with_heartbeat():
            """Run the tool while periodically sending heartbeats."""
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
                # While tool runs, emit heartbeats every few seconds
                while not tool_task.done():
                    hb = await heartbeat_content()
                    yield hb
                    await asyncio.sleep(10)  # heartbeat interval (seconds)

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

        try:
            result_text = ""
            heartbeats_v: list[StreamVariant] = []
            async for item in run_with_heartbeat():
                if isinstance(item, SVServerHint):
                    yield item  # Stream heartbeat ServerHint variants
                    heartbeats_v.append(item)
                elif isinstance(item, str):
                    # The function returns the final tool result as last value
                    result_text = item
        except Exception as e:
            log.exception("Tool %s failed", name)
            result_text = json.dumps({"error": str(e)})

        tool_out_v: list[StreamVariant] = []
        tool_msgs: list[OpenAIMessage] = []
        # Parsing tool call output as StreamVariants and messages to model
        for r in parse_tool_result(
            result_text, tool_name=name, call_id=id, thread_id=thread_id
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
    user_v = SVUser(content=user_input or "")
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
