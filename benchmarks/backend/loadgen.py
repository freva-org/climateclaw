"""Closed-loop load generator for ``POST /api/chatbot/streamresponse``.

For each concurrency level, N virtual users send requests back to back for a
fixed duration. Every request starts a new thread and reads the full NDJSON
stream. Reports throughput, time to first byte and full-response latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid

import aiohttp


async def one_request(
    session: aiohttp.ClientSession, url: str, headers: dict, store: bool
) -> tuple[float, float, bool, str]:
    body = {"thread_id": uuid.uuid4().hex, "input": "benchmark", "store_thread": store}
    t0 = time.perf_counter()
    ttfb = None
    lines = []
    async with session.post(url, json=body, headers=headers) as resp:
        async for line in resp.content:
            if ttfb is None:
                ttfb = time.perf_counter() - t0
            lines.append(line)
        total = time.perf_counter() - t0
        if resp.status != 200:
            return ttfb or total, total, False, f"HTTP {resp.status}"
    variants = [json.loads(x).get("variant") for x in lines if x.strip()]
    if "ServerError" in variants or "OpenAIError" in variants:
        return ttfb or total, total, False, "error variant in stream"
    return ttfb or total, total, True, ""


async def run_level(args, concurrency: int) -> dict:
    url = f"{args.backend}/api/chatbot/streamresponse"
    headers = {"Authorization": "Bearer bench", "x-freva-rest-url": args.upstream}
    connector = aiohttp.TCPConnector(limit=0)
    timeout = aiohttp.ClientTimeout(total=120)
    ok_ttfb: list[float] = []
    ok_total: list[float] = []
    errors: dict[str, int] = {}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        warm_end = time.perf_counter() + args.warmup
        end = warm_end + args.duration

        async def user():
            while (now := time.perf_counter()) < end:
                try:
                    ttfb, total, ok, err = await one_request(
                        s, url, headers, args.store
                    )
                except Exception as e:  # connection errors, timeouts
                    ttfb, total, ok, err = 0.0, 0.0, False, type(e).__name__
                if now < warm_end:
                    continue
                if ok:
                    ok_ttfb.append(ttfb)
                    ok_total.append(total)
                else:
                    errors[err] = errors.get(err, 0) + 1

        await asyncio.gather(*(user() for _ in range(concurrency)))

    def pct(xs: list[float], p: float) -> float:
        if not xs:
            return float("nan")
        return statistics.quantiles(xs, n=100, method="inclusive")[p - 1] * 1000

    return {
        "concurrency": concurrency,
        "ok": len(ok_total),
        "errors": errors,
        "rps": len(ok_total) / args.duration,
        "ttfb_p50_ms": pct(ok_ttfb, 50),
        "p50_ms": pct(ok_total, 50),
        "p95_ms": pct(ok_total, 95),
        "p99_ms": pct(ok_total, 99),
    }


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", default="http://127.0.0.1:8502")
    p.add_argument("--upstream", default="http://127.0.0.1:9100")
    p.add_argument("-c", "--concurrency", default="1,4,16,64,128")
    p.add_argument("-d", "--duration", type=float, default=20.0)
    p.add_argument("--warmup", type=float, default=3.0)
    p.add_argument(
        "--no-store",
        dest="store",
        action="store_false",
        help="Send store_thread=false (skip saving + topic summary).",
    )
    p.add_argument("--json", help="Also write results to this file.")
    p.add_argument("--markdown", help="Also append a Markdown table to this file.")
    args = p.parse_args()

    results = []
    print(
        f"{'conc':>5} {'req/s':>8} {'ok':>7} {'err':>5} "
        f"{'ttfb50':>8} {'p50':>8} {'p95':>8} {'p99':>8}  (ms)"
    )
    for c in (int(x) for x in args.concurrency.split(",")):
        r = await run_level(args, c)
        results.append(r)
        n_err = sum(r["errors"].values())
        print(
            f"{c:>5} {r['rps']:>8.1f} {r['ok']:>7} {n_err:>5} "
            f"{r['ttfb_p50_ms']:>8.1f} {r['p50_ms']:>8.1f} "
            f"{r['p95_ms']:>8.1f} {r['p99_ms']:>8.1f}"
            + (f"  {r['errors']}" if n_err else ""),
            flush=True,
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
    if args.markdown:
        with open(args.markdown, "a") as f:
            f.write(
                "| users | req/s | ok | errors | TTFB p50 (ms) "
                "| p50 (ms) | p95 (ms) | p99 (ms) |\n"
                "|---:|---:|---:|---:|---:|---:|---:|---:|\n"
            )
            for r in results:
                f.write(
                    f"| {r['concurrency']} | {r['rps']:.1f} | {r['ok']} "
                    f"| {sum(r['errors'].values())} | {r['ttfb_p50_ms']:.1f} "
                    f"| {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['p99_ms']:.1f} |\n"
                )


if __name__ == "__main__":
    asyncio.run(main())
