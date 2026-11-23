import asyncio

from climateclaw.tools.models import GenericToolResult
from climateclaw.tools.reference import server


async def test_consult_references_returns_typed_error_when_references_are_empty(
    monkeypatch,
):
    monkeypatch.setattr(server, "current_ids", lambda: ("session", "request"))
    monkeypatch.setattr(server, "get_model", lambda: "test-model")

    result = await server.consult_references("question", [])

    assert result == GenericToolResult(
        error=(
            "Missing tool call parameter: references. "
            "At least one reference must be selected."
        )
    )


async def test_consult_references_returns_typed_result(monkeypatch):
    class CompletedRequest:
        def raise_if_cancelled(self):
            return None

        class Cancelled:
            async def wait(self):
                await asyncio.Event().wait()

        cancelled_async = Cancelled()

    class TrackedRequest:
        async def __aenter__(self):
            return CompletedRequest()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_acomplete(**kwargs):
        assert kwargs["model"] == "test-model"
        assert kwargs["messages"][0]["content"][0] == {
            "type": "input_file",
            "file_url": "https://example.com/reference.pdf",
        }
        return {"output": "response"}

    class Reference:
        id = "test-reference"
        url = "https://example.com/reference.pdf"

    monkeypatch.setattr(server, "current_ids", lambda: ("session", "request"))
    monkeypatch.setattr(server, "get_model", lambda: "test-model")
    monkeypatch.setattr(server, "get_reference", lambda reference_id: Reference())
    monkeypatch.setattr(server, "tracked_request", lambda sid, rid: TrackedRequest())
    monkeypatch.setattr(server, "acomplete", fake_acomplete)
    monkeypatch.setattr(server, "first_text", lambda response: "answer")

    result = await server.consult_references("question", ["test-reference"])

    assert result == GenericToolResult(
        result="answer\n Citations: ['https://example.com/reference.pdf']"
    )
