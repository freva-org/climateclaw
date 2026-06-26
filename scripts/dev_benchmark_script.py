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


import asyncio
import json
import time
from dataclasses import dataclass

from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.prompting import get_entire_prompt
from climateclaw.services.service_factory import DevAuthenticator
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
from climateclaw.services.streaming.stream_variants import from_sv_to_json

"""
Headless dev/benchmark runner mirroring /chatbot/streamresponse behaviour.

- Config below (no argparse).
- Disk-only persistence (never Mongo).
- RUNS/CONCURRENCY for benchmarks; set CONCURRENCY=1 for clean mode.
- Uses ONE global McpManager; orchestrator ties MCP session to thread_id.
"""

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MODEL = "gpt-4.1"
USER_ID = "dev_user"
PROMPT = "Make an annual mean temperature global map plot for the year 2023"

RUNS = 3
CONCURRENCY = 3  # ← set to 1 for clean mode

PRINT_STREAM = False
PRINT_PER_RUN_SUMMARY = True
PRINT_FINAL_SUMMARY = True

# ──────────────────────────────────────────────────────────────────────────────

log = configure_logging("dev_script")


@dataclass
class RunResult:
    idx: int
    thread_id: str
    duration_s: float
    chunks: int
    chars: int
    status: str


async def _run_once(idx: int, sem: asyncio.Semaphore) -> RunResult:
    async with sem:
        thread_id = await new_thread_id()
        read_history = False

        Auth = await DevAuthenticator.build(None)
        Storage = await ThreadStorage.create()
        create_dir_at_cache(USER_ID, thread_id)

        await prepare_for_stream(
            thread_id=thread_id,
            user_id=USER_ID,
            Auth=Auth,
            Storage=Storage,
            read_history=read_history,
        )

        system_prompt = get_entire_prompt(USER_ID, thread_id, MODEL)

        t0 = time.perf_counter()
        chunk_count = 0
        char_count = 0
        status = "Done"

        try:
            async for variant in run_stream(
                model=MODEL,
                thread_id=thread_id,
                user_input=PROMPT,
                system_prompt=system_prompt,  # ← reuse single McpManager
            ):
                if getattr(variant, "variant", None) == "Assistant":
                    txt = getattr(variant, "text", "") or ""
                    chunk_count += 1
                    char_count += len(txt)

                if PRINT_STREAM and getattr(variant, "variant", None) != "Assistant":
                    print(json.dumps(from_sv_to_json(variant), ensure_ascii=False))

            await end_and_save_conversation(thread_id, Storage)

        except asyncio.CancelledError:
            status = "Cancelled"
        except Exception as e:
            status = f"Error:{e}"

        duration = time.perf_counter() - t0

        if PRINT_PER_RUN_SUMMARY:
            print(
                f"[run {idx:03d}] thread={thread_id} status={status} "
                f"chunks={chunk_count} chars={char_count} time={duration:.3f}s"
            )

        return RunResult(idx, thread_id, duration, chunk_count, char_count, status)


async def main() -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(_run_once(i, sem)) for i in range(RUNS)]
    results = await asyncio.gather(*tasks)

    if PRINT_FINAL_SUMMARY and results:
        ok = [r for r in results if r.status == "Done"]
        errs = [r for r in results if r.status != "Done"]
        avg = sum(r.duration_s for r in results) / len(results)
        p50 = sorted(r.duration_s for r in results)[len(results) // 2]
        fastest = min(results, key=lambda r: r.duration_s)
        slowest = max(results, key=lambda r: r.duration_s)
        total_chunks = sum(r.chunks for r in results)
        total_chars = sum(r.chars for r in results)

        print("\n=== Summary ===")
        print(f"model={MODEL} runs={RUNS} concurrency={CONCURRENCY}")
        print(f"success={len(ok)} errors={len(errs)}")
        print(
            f"avg_time={avg:.3f}s p50_time={p50:.3f}s fastest={fastest.duration_s:.3f}s slowest={slowest.duration_s:.3f}s"
        )
        print(f"total_chunks={total_chunks} total_chars={total_chars}")
        if errs:
            print("errors:")
            for r in errs[:10]:
                print(f"  run={r.idx} thread={r.thread_id} status={r.status}")


if __name__ == "__main__":
    asyncio.run(main())
