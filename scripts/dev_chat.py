from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ["CLIMATECLAW_DEV"] = "1"
os.environ["CLIMATECLAW_LITE_LLM_ADDRESS"] = "http://localhost:4000"
os.environ["CLIMATECLAW_RAG_SERVER_URL"] = "http://localhost:8050"
os.environ["CLIMATECLAW_CODE_SERVER_URL"] = "http://localhost:8051"
os.environ["CLIMATECLAW_WEB_SEARCH_SERVER_URL"] = "http://localhost:8052"
os.environ["CLIMATECLAW_MONGODB_HOST"] = "localhost"


import asyncio
import logging
from typing import Any

from climateclaw.api.chatbot.streamresponse import _sse_data
from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.prompting import get_entire_prompt
from climateclaw.core.settings import get_settings
from climateclaw.services.authentication.auth import Authenticator
from climateclaw.services.storage.helpers import create_dir_at_cache
from climateclaw.services.storage.mongodb_storage import ThreadStorage
from climateclaw.services.streaming.active_conversations import (
    end_and_save_conversation,
    new_thread_id,
)
from climateclaw.services.streaming.stream_orchestrator import (
    prepare_for_stream,
    run_stream,
)
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVCode,
    from_sv_to_json,
)

"""
Interactive multi-turn dev runner mirroring /chatbot/streamresponse behaviour.

- Reuses the same thread_id across turns (like a real conversation).
- Uses ONE global McpManager, initialized once.
- Persists to the same on-disk thread file that the orchestrator uses.
- Type '/new' to start a fresh conversation (new thread_id).
- Type '/exit' (or press Ctrl-D) to quit; the thread is already saved incrementally.

Notes:
- We rely on the stream orchestrator's existing persistence to append variants
  to the thread on disk; we create the user/thread directory before first turn.
- Printing: Assistant chunks are streamed as they arrive; non-Assistant variants
  are printed compactly when PRINT_DEBUG=True.
"""

log = configure_logging("dev_chat")
logging.getLogger("src").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpcore.http11").setLevel(logging.WARNING)
logging.getLogger("httpcore.connection").setLevel(logging.WARNING)

settings = get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MODEL = "gpt-4.1"
USER_ID = "dev_user"

PRINT_DEBUG = False  # Print non-Assistant stream variants (ServerHint, etc.)
SHOW_STATS = True  # Show per-turn simple stats

THREAD_ID = None  # It can be set to a prev thread_id to continue the conversation

# ──────────────────────────────────────────────────────────────────────────────


async def _run_turn(
    *,
    model: str,
    thread_id: str,
    user_id: str,
    user_input: str,
    system_prompt: list[dict[str, Any]],
    storage: ThreadStorage,
) -> tuple[int, int]:
    """
    Runs a single turn through run_stream and prints Assistant output as it streams.

    Returns:
        (chunk_count, char_count) for Assistant text chunks.
    """
    chunk_count = 0
    char_count = 0

    # Stream Assistant output
    first_chunk = True
    try:
        async for variant in run_stream(
            model=model,
            thread_id=thread_id,  # ← fixed per conversation
            user_input=user_input,
            system_prompt=system_prompt,
            storage=storage,
        ):
            if isinstance(variant, SVAssistant):
                txt = getattr(variant, "text", "") or ""
                if first_chunk:
                    # Print a header once per assistant message
                    print("\nAssistant:", end=" ", flush=True)
                    first_chunk = False
                print(txt, end="", flush=True)
                chunk_count += 1
                char_count += len(txt)
            elif isinstance(variant, SVCode):
                txt = getattr(variant, "code", "") or ""
                if first_chunk:
                    # Print a header once per code variant
                    print("\nCode:", end=" ", flush=True)
                    first_chunk = False
                print(txt, end="", flush=True)
                chunk_count += 1
                char_count += len(txt)
            else:
                if PRINT_DEBUG:
                    for data in _sse_data(from_sv_to_json(variant)):
                        print("\n[debug]", data)

    except asyncio.CancelledError:
        print("\n[Cancelled]")
    except Exception as e:
        print(f"\n[Error] {e}")

    if not first_chunk:
        print()  # newline after assistant completes this turn
    return chunk_count, char_count


async def main() -> None:
    # Start with a fresh conversation
    if not THREAD_ID:
        thread_id = await new_thread_id()
        read_history = False
    else:
        thread_id = THREAD_ID
        read_history = True

    Auth = Authenticator.local(username=USER_ID)
    Storage = await ThreadStorage.create()
    create_dir_at_cache(USER_ID, thread_id)

    system_prompt = get_entire_prompt(USER_ID, thread_id, MODEL)

    print("Interactive dev chat")
    print("────────────────────")
    print("Commands: /new → new thread, /id → show thread id, /exit → quit")
    print(f"Model: {MODEL}")
    print(f"Thread: {thread_id}")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting…")
            break

        if not user_input:
            # empty line → reprompt
            continue

        # Commands
        if user_input.lower() in ("/exit", "/quit"):
            await end_and_save_conversation(thread_id, Storage)
            break
        if user_input.lower() == "/id":
            print(f"Current thread_id: {thread_id}")
            continue
        if user_input.lower().startswith("/new"):
            # Optional prefix: "/new"
            thread_id = await new_thread_id()
            await prepare_for_stream(thread_id, user_id=USER_ID, Auth=Auth)
            print(f"Started new conversation. Thread: {thread_id}")
            continue

        # Normal turn
        await prepare_for_stream(
            thread_id=thread_id,
            user_id=USER_ID,
            Auth=Auth,
            Storage=Storage,
            read_history=read_history,
        )

        t_chunks, t_chars = await _run_turn(
            model=MODEL,
            thread_id=thread_id,
            user_id=USER_ID,
            user_input=user_input,
            system_prompt=system_prompt,
            storage=Storage,
        )
        await end_and_save_conversation(thread_id, Storage)
        if SHOW_STATS:
            print(f"[turn stats] chunks={t_chunks} chars={t_chars}")

    # At this point the thread file has been incrementally written by the orchestrator.
    # We just print where it lives. (Same path used by recursively_create_dir_at_cache)
    print("\nConversation ended.")
    print(
        f"Thread saved under the user/thread directory created for: user={USER_ID}, thread_id={thread_id}"
    )


if __name__ == "__main__":
    asyncio.run(main())
