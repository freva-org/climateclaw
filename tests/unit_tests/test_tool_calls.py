import json

import pytest

from climateclaw.services.streaming.tool_calls import (
    InvalidToolArguments,
    code_variant_content,
    normalize_tool_arguments,
)

CODE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
    "additionalProperties": False,
}


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
