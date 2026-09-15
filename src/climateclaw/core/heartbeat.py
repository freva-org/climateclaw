import os
import socket
from datetime import UTC, datetime

import psutil

from climateclaw.services.streaming.stream_variants import SVServerHint


def collect_performance_metrics() -> dict:
    """
    Collect system heartbeat info: CPU, memory, process stats, and host identity.
    """
    metrics = {
        "timestamp": datetime.now(UTC),
        "hostname": socket.gethostname(),
    }

    psutil.virtual_memory()

    # Current process info
    pid = os.getpid()

    # --- Memory Info ---
    mem = psutil.virtual_memory()
    metrics["memory"] = mem.used
    metrics["total_memory"] = mem.total

    # --- CPU Info ---
    metrics["cpu_usage"] = psutil.cpu_percent(interval=None)
    # psutil isn't guaranteed to have getloadavg on all platforms.
    if hasattr(psutil, "getloadavg"):
        metrics["cpu_last_minute"] = psutil.getloadavg()[0]  # 1-minute load average

    # --- Process Tree: include self and all descendants ---
    process_list = [pid]
    found_some = True
    while found_some:
        found_some = False
        for p in psutil.process_iter(["pid", "ppid"]):
            if p.info["ppid"] in process_list and p.info["pid"] not in process_list:
                process_list.append(p.info["pid"])
                found_some = True

    # --- Aggregate CPU and memory for those processes ---
    process_cpu = 0.0
    process_memory = 0
    for p in psutil.process_iter(["pid", "cpu_percent", "memory_info"]):
        if p.info["pid"] in process_list:
            try:
                process_cpu += p.info["cpu_percent"]
                process_memory += p.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    metrics["process_cpu"] = process_cpu
    metrics["process_memory"] = process_memory

    return metrics


async def heartbeat_content():
    """
    Collects system heartbeat info — CPU, memory, process stats —
    and returns it as a ServerHint.
    """
    metrics = collect_performance_metrics()
    metrics.pop("hostname", None)

    # Return as StreamVariant::ServerHint
    return SVServerHint(content=metrics)
