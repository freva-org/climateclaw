import json

import pytest

from climateclaw.services.streaming import stream_orchestrator
from climateclaw.services.streaming.stream_orchestrator import (
    StreamState,
    stream_with_tools,
)
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVStreamEnd,
    SVToolCall,
    SVToolOutput,
)
from conftest import DummyMcpManager, register_fake_mcp


async def fake_assistant_stream():
    yield {
        "choices": [
            {
                "delta": {"content": "hello "},
                "finish_reason": None,
            }
        ]
    }
    yield {
        "choices": [
            {
                "delta": {"content": "world"},
                "finish_reason": "stop",
            }
        ]
    }


def tool_schema(tool_name, properties, required):
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"{tool_name} test tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


async def fake_tool_call_stream(tool_name, arguments):
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": arguments,
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


async def collect_stream_with_code_tool(
    *,
    patch_registry,
    patch_thread_storage,
    monkeypatch,
    thread_id,
    model,
    raw_arguments,
):
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    executed_arguments = None

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("code_interpreter", raw_arguments)

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal executed_arguments
        executed_arguments = kwargs["arguments_json"]
        return json.dumps({"structuredContent": {"stdout": "ok"}})

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model=model,
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    return items, executed_arguments


@pytest.mark.asyncio
async def test_stream_with_tools_consumes_async_iterator_response(
    patch_registry, patch_thread_storage
):
    patch_registry({"t-stream": []})

    async def fake_acomplete(**kwargs):
        assert kwargs["stream"] is True
        return fake_assistant_stream()

    items = [
        item
        async for item in stream_with_tools(
            model="test-model",
            thread_id="t-stream",
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert items == [
        SVAssistant(content="hello "),
        SVAssistant(content="world"),
        SVStreamEnd(content="Stream ended."),
    ]


@pytest.mark.asyncio
async def test_stream_with_tools_rejects_malformed_code_arguments_without_mcp_call(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-malformed-code"
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    tool_was_called = False

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("code_interpreter", '{"source": "print(1)"}')

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal tool_was_called
        tool_was_called = True
        return "{}"

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gemma4:31b",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert tool_was_called is False
    assert SVCode(content='{"code": ""}', id="call_1") in items
    code_outputs = [item for item in items if isinstance(item, SVCodeOutput)]
    assert len(code_outputs) == 1
    assert "Invalid code_interpreter arguments" in str(code_outputs[0].content)


@pytest.mark.asyncio
async def test_stream_with_tools_streams_raw_code_chunks_for_non_local_models(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    # OpenAI models can stream tool-call arguments incrementally, so the backend
    # forwards those raw chunks to the client as soon as they arrive. The arguments
    # are still normalized later before tool execution for every model. In theory,
    # if an OpenAI model streamed malformed arguments like in this test, the client
    # could fail before normalization happens. However, we keep the existing streaming
    # behavior because this has not occurred with OpenAI models in practice.

    raw_arguments = '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}'

    items, executed_arguments = await collect_stream_with_code_tool(
        patch_registry=patch_registry,
        patch_thread_storage=patch_thread_storage,
        monkeypatch=monkeypatch,
        thread_id="t-non-local-code",
        model="gpt-4.1",
        raw_arguments=raw_arguments,
    )

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert [item for item in items if isinstance(item, SVCode)] == [
        SVCode(content=raw_arguments, id="call_1")
    ]


@pytest.mark.parametrize(
    "model",
    [
        "gemma4:31b",
        "qwen3.6:latest",
        "mistral-small:latest",
    ],
)
@pytest.mark.asyncio
async def test_stream_with_tools_emits_normalized_code_later_for_local_models(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
    model,
):
    raw_arguments = '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}'

    items, executed_arguments = await collect_stream_with_code_tool(
        patch_registry=patch_registry,
        patch_thread_storage=patch_thread_storage,
        monkeypatch=monkeypatch,
        thread_id=f"t-local-code-{model}",
        model=model,
        raw_arguments=raw_arguments,
    )

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert [item for item in items if isinstance(item, SVCode)] == [
        SVCode(content='{"code": "print(1)"}', id="call_1")
    ]


@pytest.mark.asyncio
async def test_stream_with_tools_normalizes_wrapped_arguments_before_tool_execution(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-wrapped-tool"
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    executed_arguments = None

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream(
            "code_interpreter",
            '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}',
        )

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal executed_arguments
        executed_arguments = kwargs["arguments_json"]
        return json.dumps({"structuredContent": {"result": "ok"}})

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gemma4:31b",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert (
        SVCode(
            content='{"code": "print(1)"}',
            id="call_1",
        )
        in items
    )


@pytest.mark.asyncio
async def test_stream_with_tools_preserves_raw_invalid_non_code_tool_call(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-invalid-web-search"
    raw_arguments = '{"q": "climate data"}'
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "web_search",
                    {"query": {"type": "string"}},
                    ["query"],
                )
            ]
        ),
    )
    tool_was_called = False

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("web_search", raw_arguments)

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal tool_was_called
        tool_was_called = True
        return "{}"

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gpt-5",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert tool_was_called is False
    assert (
        SVToolCall(
            content=raw_arguments,
            id="call_1",
            tool_name="web_search",
        )
        in items
    )
    tool_outputs = [item for item in items if isinstance(item, SVToolOutput)]
    assert len(tool_outputs) == 1
    assert "Invalid arguments for tool web_search" in str(tool_outputs[0].content)


@pytest.mark.asyncio
async def test_run_stream_hides_upstream_error_from_client(
    patch_registry, patch_thread_storage, monkeypatch
):
    from climateclaw.core.logging_setup import set_request_id
    from climateclaw.services.streaming.stream_orchestrator import run_stream
    from climateclaw.services.streaming.stream_variants import SVServerError

    patch_registry({"t-err": []})
    set_request_id("req-123")

    async def failing_stream_with_tools(**kwargs):
        raise RuntimeError(
            "LiteLLM returned HTTP 401: Incorrect API key provided: sk-proj-****4DCj"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        stream_orchestrator, "stream_with_tools", failing_stream_with_tools
    )

    items = [
        item
        async for item in run_stream(
            model="gpt-4.1",
            thread_id="t-err",
            user_input="hi",
            system_prompt=[],
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    errors = [i for i in items if isinstance(i, SVServerError)]
    assert len(errors) == 1
    assert "req-123" in errors[0].content
    for leaked in ("401", "API key", "sk-", "LiteLLM"):
        assert leaked not in errors[0].content


@pytest.mark.asyncio
async def test_run_stream_suggests_another_model_on_rate_limit(
    patch_registry, patch_thread_storage, monkeypatch
):
    import httpx

    from climateclaw.core.logging_setup import set_request_id
    from climateclaw.services.streaming.stream_orchestrator import run_stream
    from climateclaw.services.streaming.stream_variants import SVServerError

    patch_registry({"t-429": []})
    set_request_id("req-rl")

    async def rate_limited_stream_with_tools(**kwargs):
        request = httpx.Request("POST", "http://litellm:4000/v1/chat/completions")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError(
            "LiteLLM returned HTTP 429: RateLimitError: quota exceeded for sk-proj-x",
            request=request,
            response=response,
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        stream_orchestrator, "stream_with_tools", rate_limited_stream_with_tools
    )

    items = [
        item
        async for item in run_stream(
            model="gpt-4.1",
            thread_id="t-429",
            user_input="hi",
            system_prompt=[],
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    errors = [i for i in items if isinstance(i, SVServerError)]
    assert len(errors) == 1
    assert "gpt-4.1" in errors[0].content
    assert "another model" in errors[0].content
    assert "req-rl" in errors[0].content
    for leaked in ("429", "RateLimitError", "sk-", "LiteLLM"):
        assert leaked not in errors[0].content


def _http_error(status: int, text: str):
    import httpx

    request = httpx.Request("POST", "http://litellm:4000/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"LiteLLM returned HTTP {status}: {text}", request=request, response=response
    )


def _timeout_error():
    import httpx

    return httpx.ReadTimeout("timed out")


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        (
            lambda: _http_error(
                400,
                "litellm.ContextWindowExceededError: This model's maximum context "
                "length is 128000 tokens",
            ),
            ["too long for gpt-4.1", "new chat"],
        ),
        (
            lambda: _http_error(
                400,
                "litellm.ContentPolicyViolationError: Your request was rejected "
                "as a result of our safety system",
            ),
            ["content filter", "rephrase"],
        ),
        (
            lambda: _http_error(503, "litellm.ServiceUnavailableError: overloaded"),
            ["gpt-4.1 is not available", "another model"],
        ),
        (
            lambda: _http_error(502, "Bad Gateway"),
            ["gpt-4.1 is not available", "another model"],
        ),
        (
            lambda: _http_error(529, "litellm.InternalServerError: Overloaded"),
            ["gpt-4.1 is not available", "another model"],
        ),
        (
            lambda: _http_error(504, "litellm.Timeout: Request timed out"),
            ["gpt-4.1 took too long", "try again"],
        ),
        (_timeout_error, ["gpt-4.1 took too long", "try again"]),
        (
            lambda: _http_error(400, "litellm.BadRequestError: invalid tool schema"),
            ["Something went wrong"],
        ),
        (
            lambda: _http_error(401, "litellm.AuthenticationError: bad key sk-x"),
            ["Something went wrong"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_stream_maps_upstream_errors_to_user_messages(
    patch_registry, patch_thread_storage, monkeypatch, make_error, expected
):
    from climateclaw.core.logging_setup import set_request_id
    from climateclaw.services.streaming.stream_orchestrator import run_stream
    from climateclaw.services.streaming.stream_variants import SVServerError

    patch_registry({"t-map": []})
    set_request_id("req-map")

    async def failing_stream_with_tools(**kwargs):
        raise make_error()
        yield  # pragma: no cover

    monkeypatch.setattr(
        stream_orchestrator, "stream_with_tools", failing_stream_with_tools
    )

    items = [
        item
        async for item in run_stream(
            model="gpt-4.1",
            thread_id="t-map",
            user_input="hi",
            system_prompt=[],
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    errors = [i for i in items if isinstance(i, SVServerError)]
    assert len(errors) == 1
    for text in [*expected, "req-map"]:
        assert text in errors[0].content
    for leaked in ("HTTP", "litellm", "LiteLLM", "Error:", "sk-", "tokens"):
        assert leaked not in errors[0].content
