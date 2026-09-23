#!/usr/bin/env python3

import argparse
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import TextIO

import matplotlib.pyplot as plt
import pandas as pd
from pymongo import MongoClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from climateclaw.services.storage.mongodb_storage import get_mongodb_uri

MONGODB_URI = os.getenv("CLIMATECLAW_MONGODB_VM_URI") or get_mongodb_uri()
DATABASE = os.getenv("CLIMATECLAW_MONGODB_DATABASE_NAME", "chatbot")
COLLECTION = os.getenv("CLIMATECLAW_MONGODB_COLLECTION_NAME", "threads")

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ANALYSIS_RESULTS_DIR = SCRIPT_DIR / "analysis_results"
ANALYSIS_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot ClimateClaw user requests over time by model."
    )

    parser.add_argument(
        "--mongodb-uri",
        default=MONGODB_URI,
        help="MongoDB connection URI.",
    )
    parser.add_argument(
        "--database",
        default=DATABASE,
        help="MongoDB database name. Default: chatbot",
    )
    parser.add_argument(
        "--collection",
        default=COLLECTION,
        help="MongoDB collection name. Default: threads",
    )
    parser.add_argument(
        "--bucket",
        default="15min",
        help=("Time bucket size, e.g. 1min, 5min, 15min, 1h, 2h, 1d. Default: 15min"),
    )
    parser.add_argument(
        "--start",
        default="2026-09-19T16:00:00Z",
        help="Only include requests after this time, e.g. 2026-09-18T00:00:00Z",
    )
    parser.add_argument(
        "--end",
        help="Only include requests before this time, e.g. 2026-09-19T00:00:00Z",
    )

    return parser.parse_args()


def parse_bucket(bucket: str) -> tuple[int, str]:
    """
    Convert values such as:
        5min -> (5, "minute")
        1h   -> (1, "hour")
        2d   -> (2, "day")
    """
    match = re.fullmatch(
        r"(\d+)(min|m|h|d)",
        bucket.lower(),
    )

    if not match:
        raise ValueError(
            f"Invalid bucket size: {bucket!r}. "
            "Use values such as 5min, 15min, 1h, 2h, or 1d."
        )

    size = int(match.group(1))
    suffix = match.group(2)

    units = {
        "min": "minute",
        "m": "minute",
        "h": "hour",
        "d": "day",
    }

    return size, units[suffix]


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")

    return timestamp.to_pydatetime()


def build_requests_pipeline(
    bucket_size: int,
    bucket_unit: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict]:
    timestamp_filter = {"$type": "date"}

    if start is not None:
        timestamp_filter["$gte"] = start

    if end is not None:
        timestamp_filter["$lt"] = end

    return [
        {"$unwind": "$content"},
        {
            "$match": {
                "content.variant": "User",
                "content.timestamp": timestamp_filter,
            }
        },
        {
            "$group": {
                "_id": {
                    "bucket": {
                        "$dateTrunc": {
                            "date": "$content.timestamp",
                            "unit": bucket_unit,
                            "binSize": bucket_size,
                            "timezone": "UTC",
                        }
                    },
                    "model": {
                        "$ifNull": [
                            "$content.model",
                            "unknown",
                        ]
                    },
                },
                "requests": {"$sum": 1},
            }
        },
        {"$sort": {"_id.bucket": 1}},
    ]


def build_active_users_pipeline(
    bucket_size: int,
    bucket_unit: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict]:
    timestamp_filter = {"$type": "date"}

    if start is not None:
        timestamp_filter["$gte"] = start

    if end is not None:
        timestamp_filter["$lt"] = end

    return [
        {"$unwind": "$content"},
        {
            "$match": {
                "content.variant": "User",
                "content.timestamp": timestamp_filter,
            }
        },
        # One entry per user per time bucket.
        {
            "$group": {
                "_id": {
                    "bucket": {
                        "$dateTrunc": {
                            "date": "$content.timestamp",
                            "unit": bucket_unit,
                            "binSize": bucket_size,
                            "timezone": "UTC",
                        }
                    },
                    "user_id": "$user_id",
                }
            }
        },
        # Count distinct users in each bucket.
        {
            "$group": {
                "_id": "$_id.bucket",
                "active_users": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_user_report_pipeline(
    start: datetime | None,
    end: datetime | None,
) -> list[dict]:
    timestamp_filter = {"$type": "date"}

    if start is not None:
        timestamp_filter["$gte"] = start

    if end is not None:
        timestamp_filter["$lt"] = end

    return [
        {"$unwind": "$content"},
        {
            "$match": {
                "content.variant": "User",
                "content.timestamp": timestamp_filter,
            }
        },
        # Count requests per user + conversation.
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "thread_id": "$thread_id",
                },
                "requests": {"$sum": 1},
            }
        },
        {
            "$sort": {
                "_id.user_id": 1,
                "requests": -1,
            }
        },
    ]


def load_user_report(
    collection,
    start: datetime | None,
    end: datetime | None,
) -> pd.DataFrame:
    rows = list(
        collection.aggregate(
            build_user_report_pipeline(
                start=start,
                end=end,
            )
        )
    )

    if not rows:
        return pd.DataFrame(
            columns=[
                "user_id",
                "thread_id",
                "requests",
            ]
        )

    return pd.DataFrame(
        {
            "user_id": [row["_id"]["user_id"] for row in rows],
            "thread_id": [row["_id"]["thread_id"] for row in rows],
            "requests": [row["requests"] for row in rows],
        }
    )


def prepare_user_summary(
    report: pd.DataFrame,
) -> pd.DataFrame:
    if report.empty:
        return pd.DataFrame(
            columns=[
                "user_id",
                "conversations",
                "requests",
                "avg_requests_per_conversation",
                "min_requests_per_conversation",
                "max_requests_per_conversation",
            ]
        )

    summary = (
        report.groupby("user_id")
        .agg(
            conversations=("thread_id", "nunique"),
            requests=("requests", "sum"),
            avg_requests_per_conversation=("requests", "mean"),
            min_requests_per_conversation=("requests", "min"),
            max_requests_per_conversation=("requests", "max"),
        )
        .reset_index()
    )

    summary["avg_requests_per_conversation"] = summary[
        "avg_requests_per_conversation"
    ].round(2)

    return summary.sort_values(
        ["conversations", "requests"],
        ascending=False,
    )


def load_requests(
    collection,
    bucket_size: int,
    bucket_unit: str,
    start: datetime | None,
    end: datetime | None,
) -> pd.DataFrame:
    pipeline = build_requests_pipeline(
        bucket_size=bucket_size,
        bucket_unit=bucket_unit,
        start=start,
        end=end,
    )

    rows = list(collection.aggregate(pipeline))

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "timestamp": [row["_id"]["bucket"] for row in rows],
            "model": [row["_id"]["model"] for row in rows],
            "requests": [row["requests"] for row in rows],
        }
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        utc=True,
    )

    return df


def load_active_users(
    collection,
    bucket_size: int,
    bucket_unit: str,
    start: datetime | None,
    end: datetime | None,
) -> pd.Series:
    pipeline = build_active_users_pipeline(
        bucket_size=bucket_size,
        bucket_unit=bucket_unit,
        start=start,
        end=end,
    )

    rows = list(collection.aggregate(pipeline))

    if not rows:
        return pd.Series(
            dtype=int,
            name="active_users",
        )

    series = pd.Series(
        data=[row["active_users"] for row in rows],
        index=pd.to_datetime(
            [row["_id"] for row in rows],
            utc=True,
        ),
        name="active_users",
        dtype=int,
    )

    return series


def load_total_unique_users(
    collection,
    start: datetime | None,
    end: datetime | None,
) -> int:
    timestamp_filter = {"$type": "date"}

    if start is not None:
        timestamp_filter["$gte"] = start

    if end is not None:
        timestamp_filter["$lt"] = end

    pipeline = [
        {"$unwind": "$content"},
        {
            "$match": {
                "content.variant": "User",
                "content.timestamp": timestamp_filter,
            }
        },
        {
            "$group": {
                "_id": "$user_id",
            }
        },
        {
            "$count": "unique_users",
        },
    ]

    rows = list(collection.aggregate(pipeline))

    if not rows:
        return 0

    return rows[0]["unique_users"]


def prepare_plot_data(
    df: pd.DataFrame,
    active_users: pd.Series,
    bucket: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[pd.DataFrame, pd.Series]:
    requests = df.pivot_table(
        index="timestamp",
        columns="model",
        values="requests",
        aggfunc="sum",
        fill_value=0,
    )

    # Put the most frequently used models first.
    model_order = requests.sum().sort_values(ascending=False).index

    requests = requests[model_order]

    if start is not None:
        range_start = pd.Timestamp(start)
    else:
        range_start = requests.index.min()

    if end is not None:
        range_end = pd.Timestamp(end)
    else:
        range_end = requests.index.max()

    # Align the requested range with the configured bucket boundaries.
    range_start = range_start.floor(bucket)
    range_end = range_end.floor(bucket)

    full_index = pd.date_range(
        start=range_start,
        end=range_end,
        freq=bucket,
        tz="UTC",
    )

    requests = requests.reindex(
        full_index,
        fill_value=0,
    )

    active_users = active_users.reindex(
        full_index,
        fill_value=0,
    )

    return requests, active_users


def print_user_report(
    report: pd.DataFrame,
    file: TextIO = sys.stdout,
) -> None:
    if report.empty:
        print("No user activity found for report.")
        return

    summary = prepare_user_summary(report)

    print(file=file)
    print("USER ACTIVITY REPORT", file=file)
    print("--------------------", file=file)

    for _, user in summary.iterrows():
        user_id = user["user_id"]

        print(file=file)
        print(f"User: {user_id}", file=file)
        print(f"  Conversations: {int(user['conversations'])}", file=file)
        print(f"  Requests:      {int(user['requests'])}", file=file)
        print(
            "  Requests/conversation: "
            f"{user['avg_requests_per_conversation']:.2f} average",
            file=file,
        )

        conversations = report[report["user_id"] == user_id].sort_values(
            "requests",
            ascending=False,
        )

        for _, conversation in conversations.iterrows():
            print(
                f"    {conversation['thread_id']}: "
                f"{int(conversation['requests'])} requests",
                file=file,
            )

    print(file=file)


def print_summary(
    requests: pd.DataFrame,
    active_users: pd.Series,
    total_unique_users: int,
    file: TextIO = sys.stdout,
) -> None:
    per_model = requests.sum().sort_values(ascending=False)

    total_requests = int(per_model.sum())

    print(file=file)
    print("REQUESTS BY MODEL", file=file)
    print("-----------------", file=file)

    for model, count in per_model.items():
        percentage = count / total_requests * 100 if total_requests else 0

        print(f"{model:30} {int(count):8,d} ({percentage:5.1f}%)", file=file)

    print("-----------------", file=file)
    print(f"{'Total requests':30} {total_requests:8,d}", file=file)
    print(f"{'Unique users':30} {total_unique_users:8,d}", file=file)
    print(f"{'Peak active users':30} {int(active_users.max()):8,d}", file=file)
    print(file=file)


def plot_requests(
    requests: pd.DataFrame,
    active_users: pd.Series,
    bucket: str,
    total_unique_users: int,
) -> None:
    fig, requests_ax = plt.subplots(figsize=(16, 7))

    bar_width = pd.Timedelta(bucket) * 0.9

    bottom = pd.Series(
        0,
        index=requests.index,
        dtype=float,
    )

    # Requests by model
    for model in requests.columns:
        requests_ax.bar(
            requests.index,
            requests[model],
            width=bar_width,
            bottom=bottom,
            label=model,
        )

        bottom += requests[model]

    # Active users
    active_users_ax = requests_ax.twinx()
    nonzero_active_users = active_users[active_users > 0]

    active_users_ax.scatter(
        nonzero_active_users.index,
        nonzero_active_users.values,
        color="black",
        s=30,
        zorder=3,
        label="Active users",
    )

    # Use the same scale for BOTH y-axes.
    max_requests = int(requests.sum(axis=1).max()) if not requests.empty else 0

    max_active_users = int(active_users.max()) if not active_users.empty else 0

    y_max = max(
        max_requests,
        max_active_users,
        1,
    )

    # Leave a little room above the highest value.
    y_max *= 1.1

    requests_ax.set_ylim(0, y_max)

    active_users_ax.set_ylim(0, y_max)

    # Labels
    total_requests = int(requests.sum().sum())

    requests_ax.set_title(
        "ClimateClaw usage\n"
        f"{total_requests:,} requests · "
        f"{total_unique_users:,} unique users · "
        f"{bucket} buckets"
    )

    requests_ax.set_xlabel("Time (UTC)")
    requests_ax.set_ylabel("Requests")

    active_users_ax.set_ylabel(
        "Active users",
        color="black",
    )

    active_users_ax.tick_params(
        axis="y",
        colors="black",
    )

    # Legends
    request_handles, request_labels = requests_ax.get_legend_handles_labels()

    user_handles, user_labels = active_users_ax.get_legend_handles_labels()

    requests_ax.legend(
        request_handles + user_handles,
        request_labels + user_labels,
        title="Metric / model",
        bbox_to_anchor=(1.08, 1),
        loc="upper left",
    )

    # Styling
    requests_ax.grid(
        axis="y",
        alpha=0.3,
    )

    fig.autofmt_xdate()
    fig.tight_layout()

    output_path = ANALYSIS_RESULTS_DIR / "usage_analysis.png"

    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
    )

    print(f"Saved usage plot to {output_path}")


def plot_runtime_metrics(
    db,
    start: datetime | None,
    end: datetime | None,
) -> None:
    collection = db["runtime_metrics"]

    rows = list(
        collection.find(
            {},
            {
                "_id": 0,
                "timestamp": 1,
                "hostname": 1,
                "service_hostname": 1,
                "memory": 1,
                "total_memory": 1,
                "cpu_usage": 1,
                "cpu_last_minute": 1,
                "process_cpu": 1,
                "process_memory": 1,
            },
        )
    )

    if not rows:
        print("No runtime metrics found.")
        return

    df = pd.DataFrame(rows)

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        utc=True,
        errors="coerce",
    )

    df = df.dropna(
        subset=[
            "timestamp",
            "hostname",
            "service_hostname",
        ]
    )

    if start is not None:
        df = df[df["timestamp"] >= pd.Timestamp(start)]

    if end is not None:
        df = df[df["timestamp"] < pd.Timestamp(end)]

    if df.empty:
        print("No runtime metrics found in selected time range.")
        return

    df = df.sort_values(
        [
            "hostname",
            "service_hostname",
            "timestamp",
        ]
    )

    # Convert memory values to GiB.
    df["memory_gib"] = (
        pd.to_numeric(
            df["memory"],
            errors="coerce",
        )
        / 1024**3
    )

    df["total_memory_gib"] = (
        pd.to_numeric(
            df["total_memory"],
            errors="coerce",
        )
        / 1024**3
    )

    df["process_memory_gib"] = (
        pd.to_numeric(
            df["process_memory"],
            errors="coerce",
        )
        / 1024**3
    )

    # Create one CPU + memory plot for each physical host.
    for hostname, host_df in df.groupby("hostname"):
        # CPU
        fig, ax = plt.subplots(figsize=(16, 7))

        # cpu_usage is a HOST metric.
        # Every ClimateClaw service running on this host records essentially
        # the same value, so collapse the duplicate measurements.
        host_cpu = (
            host_df.set_index("timestamp")["cpu_usage"].resample("1s").mean().dropna()
        )

        ax.plot(
            host_cpu.index,
            host_cpu.values,
            color="black",
            linewidth=2.5,
            label=f"{hostname} — host CPU",
            zorder=10,
        )

        # process_cpu belongs to an individual ClimateClaw service.
        for service_hostname, service_df in host_df.groupby("service_hostname"):
            ax.plot(
                service_df["timestamp"],
                service_df["process_cpu"],
                linewidth=1.5,
                label=service_hostname,
            )

        ax.set_title(f"CPU usage — {hostname}")

        ax.set_xlabel("Time (UTC)")

        ax.set_ylabel("CPU usage (%)")

        ax.set_ylim(bottom=0)

        ax.grid(axis="y", alpha=0.3)

        ax.legend(
            title="Host / ClimateClaw service",
            bbox_to_anchor=(1.02, 1),
            loc="upper left",
        )

        fig.autofmt_xdate()
        fig.tight_layout()

        safe_hostname = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            hostname,
        )

        output_path = ANALYSIS_RESULTS_DIR / f"runtime_cpu_{safe_hostname}.png"

        fig.savefig(
            output_path,
            dpi=150,
            bbox_inches="tight",
        )

        print(f"Saved CPU plot to {output_path}")

        # Memory
        fig, ax = plt.subplots(figsize=(16, 7))

        # Host memory, again de-duplicate measurements coming
        # from the individual services.
        host_memory = (
            host_df.set_index("timestamp")["memory_gib"].resample("1s").mean().dropna()
        )

        ax.plot(
            host_memory.index,
            host_memory.values,
            color="black",
            linewidth=2.5,
            label=f"{hostname} — host memory",
            zorder=10,
        )

        # Individual ClimateClaw service memory.
        for service_hostname, service_df in host_df.groupby("service_hostname"):
            ax.plot(
                service_df["timestamp"],
                service_df["process_memory_gib"],
                linewidth=1.5,
                label=service_hostname,
            )

        ax.set_title(f"Memory usage — {hostname}")

        ax.set_xlabel("Time (UTC)")

        ax.set_ylabel("Memory (GiB)")

        ax.set_ylim(bottom=0)

        ax.grid(
            axis="y",
            alpha=0.3,
        )

        ax.legend(
            title="Host / ClimateClaw service",
            bbox_to_anchor=(1.02, 1),
            loc="upper left",
        )

        fig.autofmt_xdate()
        fig.tight_layout()

        output_path = ANALYSIS_RESULTS_DIR / f"runtime_memory_{safe_hostname}.png"

        fig.savefig(
            output_path,
            dpi=150,
            bbox_inches="tight",
        )

        print(f"Saved memory plot to {output_path}")


def save_text_report(
    requests: pd.DataFrame,
    active_users: pd.Series,
    total_unique_users: int,
    user_report: pd.DataFrame,
) -> None:
    output_path = ANALYSIS_RESULTS_DIR / "usage_report.txt"

    with output_path.open("w", encoding="utf-8") as file:
        print_summary(
            requests=requests,
            active_users=active_users,
            total_unique_users=total_unique_users,
            file=file,
        )

        print_user_report(
            report=user_report,
            file=file,
        )

    print(f"Saved text report to {output_path}")


def main() -> None:
    args = parse_args()

    bucket_size, bucket_unit = parse_bucket(args.bucket)

    start = parse_datetime(args.start)

    end = parse_datetime(args.end) if args.end else datetime.now(timezone.utc)

    print(f"Query range: {start} -> {end}")

    client = MongoClient(args.mongodb_uri)
    db = client[args.database]

    try:
        collection = db[args.collection]

        requests_df = load_requests(
            collection=collection,
            bucket_size=bucket_size,
            bucket_unit=bucket_unit,
            start=start,
            end=end,
        )

        if requests_df.empty:
            print("No user requests found.")
            return

        active_users = load_active_users(
            collection=collection,
            bucket_size=bucket_size,
            bucket_unit=bucket_unit,
            start=start,
            end=end,
        )

        total_unique_users = load_total_unique_users(
            collection=collection,
            start=start,
            end=end,
        )

        requests, active_users = prepare_plot_data(
            df=requests_df,
            active_users=active_users,
            bucket=args.bucket,
            start=start,
            end=end,
        )

        user_report = load_user_report(
            collection=collection,
            start=start,
            end=end,
        )

        plot_requests(
            requests=requests,
            active_users=active_users,
            bucket=args.bucket,
            total_unique_users=total_unique_users,
        )

        plot_runtime_metrics(
            db=db,
            start=start,
            end=end,
        )

        save_text_report(
            requests=requests,
            active_users=active_users,
            total_unique_users=total_unique_users,
            user_report=user_report,
        )

    finally:
        client.close()


if __name__ == "__main__":
    main()
