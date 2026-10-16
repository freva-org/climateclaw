"""Render a base-vs-head comparison of two ``loadgen.py --json`` results.

Writes Markdown (table + Mermaid charts) to stdout, for a PR comment or
the GitHub job summary.
"""

from __future__ import annotations

import argparse
import json
import math

# Hidden marker so the workflow can find and update its own PR comment.
MARKER = "<!-- climateclaw-backend-benchmark -->"


def _load(path: str) -> dict[int, dict]:
    with open(path) as f:
        return {r["concurrency"]: r for r in json.load(f)}


def _fmt(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x:.1f}"


def _change(base: float, head: float, higher_is_better: bool) -> str:
    if math.isnan(base) or math.isnan(head) or base == 0:
        return ""
    pct = (head - base) / base * 100
    better = pct > 0 if higher_is_better else pct < 0
    icon = "🟢" if better and abs(pct) >= 5 else "🔴" if abs(pct) >= 5 else "⚪"
    return f"{icon} {pct:+.0f}%"


def _chart(title: str, y_label: str, users: list[int], base: list, head: list):
    def series(xs: list[float]) -> str:
        return "[" + ", ".join("0" if math.isnan(x) else f"{x:.1f}" for x in xs) + "]"

    return "\n".join(
        [
            "```mermaid",
            "xychart-beta",
            f'    title "{title}"',
            f'    x-axis "concurrent users" [{", ".join(map(str, users))}]',
            f'    y-axis "{y_label}"',
            f"    line {series(base)}",
            f"    line {series(head)}",
            "```",
        ]
    )


def render(
    base: dict[int, dict], head: dict[int, dict], base_name: str, head_name: str
):
    users = sorted(set(base) & set(head))
    out = [
        MARKER,
        "## Backend benchmark",
        "",
        f"`{base_name}` (base) vs `{head_name}` (this change), run back to back on "
        "the same runner. Dependencies are instant stand-ins, so this measures the "
        "backend's own cost. Changes under 5% are within noise.",
        "",
        "| users | req/s base | req/s head | change | p95 base (ms) "
        "| p95 head (ms) | change | errors (base/head) |",
        "|---:|---:|---:|:--|---:|---:|:--|:--:|",
    ]
    for u in users:
        b, h = base[u], head[u]
        out.append(
            f"| {u} | {_fmt(b['rps'])} | {_fmt(h['rps'])} "
            f"| {_change(b['rps'], h['rps'], True)} "
            f"| {_fmt(b['p95_ms'])} | {_fmt(h['p95_ms'])} "
            f"| {_change(b['p95_ms'], h['p95_ms'], False)} "
            f"| {sum(b['errors'].values())}/{sum(h['errors'].values())} |"
        )
    out += [
        "",
        "Charts: first line = base, second line = head.",
        "",
        _chart(
            "Throughput (higher is better)",
            "req/s",
            users,
            [base[u]["rps"] for u in users],
            [head[u]["rps"] for u in users],
        ),
        "",
        _chart(
            "p95 latency (lower is better)",
            "ms",
            users,
            [base[u]["p95_ms"] for u in users],
            [head[u]["p95_ms"] for u in users],
        ),
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("base_json")
    p.add_argument("head_json")
    p.add_argument("--base-name", default="base")
    p.add_argument("--head-name", default="head")
    args = p.parse_args()
    print(
        render(
            _load(args.base_json), _load(args.head_json), args.base_name, args.head_name
        ),
        end="",
    )


if __name__ == "__main__":
    main()
