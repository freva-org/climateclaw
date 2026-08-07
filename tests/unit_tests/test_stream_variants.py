import json

from climateclaw.services.streaming.openai_helpers import help_convert_sv_ccrm
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
    assert out[2].content["stdout"] == ""


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


def test_ccrm_conversion_basic():
    conv: list[StreamVariant] = [
        SVUser(content="hi", model="gpt-4.1"),
        SVAssistant(content="hello"),
        SVStreamEnd(content="Done"),
    ]
    msgs = help_convert_sv_ccrm(conv, include_images=False, include_meta=False)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hi"
    assert "model" not in msgs[0]
    assert msgs[1]["role"] == "assistant"
    assert "stream_end" not in (m.get("name") for m in msgs if "name" in m)


def test_ccrm_codeoutput_conversion_does_not_mutate_preview_url():
    code_output = SVCodeOutput(
        content=normalize_code_output(
            {
                "stdout": "",
                "stderr": "",
                "created_files": [
                    {
                        "path": "plot.png",
                        "mime_type": "image/png",
                        "preview_url": "http://localhost/plot.png",
                    }
                ],
            }
        ),
        id="call_1",
    )

    msgs = help_convert_sv_ccrm([code_output])

    assert code_output.content["created_files"][0]["preview_url"] == (
        "http://localhost/plot.png"
    )
    model_payload = json.loads(msgs[0]["content"])
    assert "preview_url" not in model_payload["created_files"][0]


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
