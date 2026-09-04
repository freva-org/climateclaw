from climateclaw.services.streaming import tool_calls
from climateclaw.services.streaming.stream_variants import SVCodeOutput, SVImage


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
            {"mime_type": "text/plain"},
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
    assert code_output.content["created_files"][0]["preview_url"] == (
        "https://example.test/static/preview/climateclaw/thread_123/plots/figure.png"
    )
    assert code_output.content["created_files"][1]["preview_url"] == (
        "https://example.test/static/preview/climateclaw/thread_123/data.csv"
    )
    assert "preview_url" not in code_output.content["created_files"][2]


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
