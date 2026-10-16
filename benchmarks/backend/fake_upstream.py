"""Instant stand-in for everything the backend calls over HTTP.

* ``GET  /api/freva-nextgen/auth/v2/systemuser`` - Freva token check
* ``POST /v1/chat/completions``                  - LiteLLM (stream and non-stream)

Responses are sent immediately so the benchmark measures only the backend.
Run several workers (``--workers``) so this server is never the bottleneck.
"""

from __future__ import annotations

import argparse
import json
import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Number of content chunks per streamed completion (roughly tokens).
CHUNKS = int(os.getenv("BENCH_STREAM_CHUNKS", "50"))


async def systemuser(_: Request) -> Response:
    return JSONResponse({"username": "bench-user"})


def _chunk(delta: dict, finish_reason: str | None = None) -> bytes:
    body = {
        "id": "bench",
        "object": "chat.completion.chunk",
        "model": "bench",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


async def completions(request: Request) -> Response:
    payload = await request.json()
    if not payload.get("stream"):
        return JSONResponse(
            {
                "id": "bench",
                "object": "chat.completion",
                "model": "bench",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Bench topic"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def stream():
        yield _chunk({"role": "assistant", "content": ""})
        for i in range(CHUNKS):
            yield _chunk({"content": f"tok{i} "})
        yield _chunk({}, finish_reason="stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


app = Starlette(
    routes=[
        Route("/api/freva-nextgen/auth/v2/systemuser", systemuser),
        Route("/v1/chat/completions", completions, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    uvicorn.run(
        "fake_upstream:app",
        app_dir=os.path.dirname(__file__),
        host="127.0.0.1",
        port=args.port,
        workers=args.workers,
        log_level="warning",
        access_log=False,
    )
