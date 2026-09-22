from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVUser,
    cleanup_conversation,
    empty_code_interpreter_output,
    from_json_to_sv,
    from_sv_to_json,
    normalize_code_output,
    normalize_conv_for_prompt,
)


def test_cleanup_inserts_codeoutput_and_end():
    conv: list[StreamVariant] = [
        SVUser(content="hi"),
        SVCode(content="print(1)", id="call_1"),
    ]
    out = cleanup_conversation(
        conv, append_stream_end=True
    )  # default: append_stream_end=True
    # Expect: User, Code, (inserted) CodeOutput, StreamEnd
    assert isinstance(out[-1], SVStreamEnd)
    kinds = [v.variant for v in out]
    assert kinds == ["User", "Code", "CodeOutput", "StreamEnd"]
    assert isinstance(out[2], SVCodeOutput)
    assert out[2].id == "call_1"
    assert isinstance(out[2].content, dict)
    assert out[2].content["error"] == "No response was received from code-interpreter."


def test_cleanup_no_extra_end_if_existing():
    conv: list[StreamVariant] = [
        SVUser(content="hi"),
        SVCode(content="print(1)", id="call_1"),
        SVCodeOutput(content=empty_code_interpreter_output(), id="call_1"),
        SVStreamEnd(content="Done"),
    ]
    out = cleanup_conversation(conv, append_stream_end=True)
    kinds = [v.variant for v in out]
    # No duplicate StreamEnd
    assert kinds == ["User", "Code", "CodeOutput", "StreamEnd"]


def test_normalize_conv_for_prompt_filters_meta():
    conv: list[StreamVariant] = [
        SVServerHint(content={"thread_id": "abc"}),
        SVUser(content="hi"),
        SVAssistant(content="hello"),
        SVServerError(content="oops"),
        SVStreamEnd(content="Done"),
    ]
    out = normalize_conv_for_prompt(conv, include_meta=False)
    # Meta variants removed
    kinds = [v.variant for v in out]
    assert kinds == ["User", "Assistant"]


def test_code_wire_roundtrip():
    original = SVCode(content="x=1", id="cid")
    wire = from_sv_to_json(original)
    assert wire == {"variant": "Code", "content": "x=1", "id": "cid", "feedback": ""}
    back = from_json_to_sv(wire)
    assert back == original  # pydantic models are comparable


def test_user_wire_roundtrip_includes_model():
    original = SVUser(content="hi", model="gpt-4.1")
    wire = from_sv_to_json(original)
    assert wire == {"variant": "User", "content": "hi", "model": "gpt-4.1"}
    back = from_json_to_sv(wire)
    assert back == original


def test_codeoutput_wire_content_is_structured():
    original = SVCodeOutput(
        content=normalize_code_output({"stdout": "ok\n", "stderr": ""}),
        id="call_1",
    )
    wire = from_sv_to_json(original)
    assert wire["content"]["stdout"] == "ok\n"
    assert isinstance(wire["content"], dict)


def test_legacy_codeoutput_string_normalizes_to_structured_content():
    wire = {
        "variant": "CodeOutput",
        "content": '{"stdout": "ok\\n", "stderr": "", "display_data": []}',
        "id": "call_1",
    }
    back = from_json_to_sv(wire)
    assert isinstance(back, SVCodeOutput)
    assert back.content["stdout"] == "ok\n"


def test_normalize_code_output_none_returns_empty_output():
    output = normalize_code_output(None)

    assert output == empty_code_interpreter_output()


def test_legacy_codeoutput_list_normalizes_first_item_to_stdout():
    legacy = {
        "variant": "CodeOutput",
        "content": ["legacy output", "call_1"],
    }

    codeoutput_v = from_json_to_sv(legacy)

    assert isinstance(codeoutput_v, SVCodeOutput)
    assert codeoutput_v.id == "call_1"
    assert codeoutput_v.content == empty_code_interpreter_output(stdout="legacy output")


def test_normalize_code_output_strips_png_from_display_data():
    output = normalize_code_output(
        {
            "stdout": "",
            "stderr": "",
            "display_data": [
                {
                    "image/png": "base64-image",
                    "text/plain": "<Figure size 640x480>",
                }
            ],
        }
    )

    assert output["display_data"] == [{"text/plain": "<Figure size 640x480>"}]
