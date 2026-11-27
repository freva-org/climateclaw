import json

import pytest

from climateclaw.services.streaming import tool_calls
from climateclaw.services.streaming.stream_variants import (
    SVCodeOutput,
    SVImage,
)
from climateclaw.services.streaming.tool_calls import (
    InvalidToolArguments,
    accumulate_tool_calls,
    code_variant_content,
    finalize_tool_calls,
    normalize_tool_arguments,
)

CODE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
    "additionalProperties": False,
}


def _tool_call_chunk(*tool_calls):
    return {"choices": [{"delta": {"tool_calls": list(tool_calls)}}]}


def test_accumulate_tool_calls_handles_streamed_deltas_without_id():
    # Shape sent by gpt-4.1: only the first delta per index carries the ID
    # and name; later deltas have no "id" key and only append arguments.
    chunks = [
        _tool_call_chunk(
            {
                "id": "call_abc",
                "function": {"arguments": "", "name": "code_interpreter"},
                "type": "function",
                "index": 0,
            }
        ),
        _tool_call_chunk(
            {"function": {"arguments": '{"'}, "type": "function", "index": 0}
        ),
        _tool_call_chunk(
            {"function": {"arguments": 'code": "'}, "type": "function", "index": 0}
        ),
        _tool_call_chunk(
            {"function": {"arguments": 'print(1)"}'}, "type": "function", "index": 0}
        ),
    ]

    agg: dict = {}
    for chunk in chunks:
        accumulate_tool_calls(chunk, agg)

    assert finalize_tool_calls(agg) == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "code_interpreter",
                "arguments": '{"code": "print(1)"}',
            },
        }
    ]


def test_finalize_tool_calls_rejects_call_that_never_received_an_id():
    agg: dict = {}
    accumulate_tool_calls(
        _tool_call_chunk(
            {"function": {"name": "code_interpreter", "arguments": "{}"}, "index": 0}
        ),
        agg,
    )

    with pytest.raises(ValueError, match="index 0 is missing an ID"):
        finalize_tool_calls(agg)


def test_normalize_tool_arguments_accepts_direct_arguments():
    normalized = normalize_tool_arguments(
        raw_arguments='{"code": "print(1)"}',
        input_schema=CODE_SCHEMA,
    )

    assert normalized.arguments == {"code": "print(1)"}
    assert normalized.was_unwrapped is False
    assert normalized.wrapper_key is None


def test_normalize_tool_arguments_unwraps_single_valid_nested_object():
    normalized = normalize_tool_arguments(
        raw_arguments='{"args": {"code": "print(1)"}, "tool": "code_interpreter"}',
        input_schema=CODE_SCHEMA,
    )

    assert normalized.arguments == {"code": "print(1)"}
    assert normalized.was_unwrapped is True
    assert normalized.wrapper_key == "args"


@pytest.mark.parametrize(
    ("raw_arguments", "error_match"),
    [
        ('{"code":', "not a valid JSON string"),
        ('["print(1)"]', "must decode to a JSON object"),
        ('{"source": "print(1)"}', "do not match the declared input schema"),
    ],
)
def test_normalize_tool_arguments_rejects_invalid_arguments(
    raw_arguments,
    error_match,
):
    with pytest.raises(InvalidToolArguments, match=error_match):
        normalize_tool_arguments(
            raw_arguments=raw_arguments,
            input_schema=CODE_SCHEMA,
        )


def test_normalize_tool_arguments_rejects_empty_schema():
    with pytest.raises(InvalidToolArguments, match="No input schema is available"):
        normalize_tool_arguments(
            raw_arguments='{"code": "print(1)"}',
            input_schema={},
        )


def test_normalize_tool_arguments_rejects_ambiguous_wrappers():
    with pytest.raises(InvalidToolArguments, match="multiple one-level objects"):
        normalize_tool_arguments(
            raw_arguments=(
                '{"args": {"code": "print(1)"}, "arguments": {"code": "print(2)"}}'
            ),
            input_schema=CODE_SCHEMA,
        )


def test_normalize_tool_arguments_ignores_non_dict_wrappers_when_rejecting():
    with pytest.raises(InvalidToolArguments, match="do not match"):
        normalize_tool_arguments(
            raw_arguments='{"args": "print(1)", "tool": "code_interpreter"}',
            input_schema=CODE_SCHEMA,
        )


def test_code_variant_content_prefers_normalized_arguments():
    content = code_variant_content(
        raw_arguments='{"args": {"code": "print(1)"}}',
        normalized_arguments='{"code": "print(2)"}',
    )

    assert json.loads(content) == {"code": "print(2)"}


def test_code_variant_content_extracts_top_level_code():
    content = code_variant_content(raw_arguments='{"code": "print(1)"}')

    assert json.loads(content) == {"code": "print(1)"}


def test_code_variant_content_extracts_nested_code():
    content = code_variant_content(
        raw_arguments='{"args": {"code": "print(1)"}, "tool": "code_interpreter"}'
    )

    assert json.loads(content) == {"code": "print(1)"}


@pytest.mark.parametrize(
    "raw_arguments",
    [
        '{"code":',
        '["print(1)"]',
        '{"source": "print(1)"}',
    ],
)
def test_code_variant_content_falls_back_to_empty_code(raw_arguments):
    content = code_variant_content(raw_arguments=raw_arguments)

    assert json.loads(content) == {"code": ""}


def test_parse_code_interpreter_result_adds_created_file_preview_url(monkeypatch):
    monkeypatch.setattr(tool_calls, "PROJECT_WEBSITE", "https://example.test")
    result = {
        "stdout": "saved\n",
        "stderr": "",
        "result_repr": "",
        "display_data": [],
        "error": "",
        "created_files": [
            {"path": "plots/figure.png", "mime_type": "image/png"},
            {"path": "data.csv", "mime_type": "text/csv"},
        ],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    code_output = emitted[0]
    assert isinstance(code_output, SVCodeOutput)
    assert code_output.content.created_files[0].preview_url == (
        "https://example.test/static/preview/climateclaw/thread_123/plots/figure.png"
    )
    assert code_output.content.created_files[1].preview_url == (
        "https://example.test/static/preview/climateclaw/thread_123/data.csv"
    )


def test_parse_code_interpreter_result_adds_success_output_for_empty_success():
    result = {
        "stdout": "",
        "stderr": "",
        "result_repr": "",
        "display_data": [],
        "error": "",
        "created_files": [],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    code_output = emitted[0]
    assert isinstance(code_output, SVCodeOutput)
    assert code_output.content.stdout == "Execution completed successfully."
    assert emitted[-1].is_error is False


def test_parse_code_interpreter_result_suppresses_saved_display_images():
    result = {
        "stdout": "",
        "stderr": "",
        "result_repr": "",
        "display_data": [{"image/png": "base64-image"}],
        "error": "",
        "created_files": [{"path": "figure.png", "mime_type": "image/png"}],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    assert [type(item) for item in emitted] == [SVCodeOutput, tool_calls.FinalSummary]
    summary = emitted[-1]
    assert summary.var_block == [emitted[0]]


def test_parse_code_interpreter_result_streams_unsaved_display_images():
    result = {
        "stdout": "",
        "stderr": "",
        "result_repr": "",
        "display_data": [
            {"image/png": "first-base64"},
            {"text/plain": "not an image"},
            {"image/png": "second-base64"},
        ],
        "error": "",
        "created_files": [{"path": "figure.png", "mime_type": "image/png"}],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    images = [item for item in emitted if isinstance(item, SVImage)]
    assert [(image.id, image.content) for image in images] == [
        ("call_1_0", "first-base64"),
        ("call_1_2", "second-base64"),
    ]
    assert emitted[-1].var_block == [emitted[0], *images]


def test_parse_code_interpreter_result_marks_stderr_as_error():
    result = {
        "stdout": "",
        "stderr": "warning",
        "result_repr": "",
        "display_data": [],
        "error": "",
        "created_files": [],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    assert emitted[-1].is_error is True


def test_parse_code_interpreter_result_marks_error_as_error():
    result = {
        "stdout": "",
        "stderr": "",
        "result_repr": "",
        "display_data": [],
        "error": "boom",
        "created_files": [],
    }

    emitted = list(
        tool_calls.parse_code_interpreter_result(
            result,
            id="call_1",
            thread_id="thread_123",
        )
    )

    assert emitted[-1].is_error is True
