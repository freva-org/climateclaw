from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from matplotlib import pyplot as plt
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ["CLIMATECLAW_DEV"] = "1"  # "1"/"true"/"yes" means dev mode
os.environ["CLIMATECLAW_LITE_LLM_ADDRESS"] = "http://localhost:4000"
os.environ["CLIMATECLAW_RAG_SERVER_URL"] = "http://localhost:8050"
os.environ["CLIMATECLAW_CODE_SERVER_URL"] = "http://localhost:8051"
os.environ["CLIMATECLAW_WEB_SEARCH_SERVER_URL"] = "http://localhost:8052"
os.environ["CLIMATECLAW_PLUGIN_CODE_SEARCH_SERVER_URL"] = "http://localhost:8053"
os.environ["CLIMATECLAW_MONGODB_HOST"] = "localhost"

from climateclaw.core.logging_setup import configure_logging, silence_logger
from climateclaw.core.prompting import get_entire_prompt
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
    SVToolCall,
    SVToolOutput,
)

"""
Headless dev/benchmark runner mirroring /chatbot/streamresponse behaviour. Headless means that it runs without a web server, and without any user interaction. It is intended for development and benchmarking purposes.

- Config below (no argparse).
- Disk-only persistence (never Mongo).
- RUNS/CONCURRENCY for benchmarks; set CONCURRENCY=1 for clean mode.
- Uses ONE global McpManager; orchestrator ties MCP session to thread_id.
"""

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

################### IMPORTANT NOTE ###################
# in order to make this evaluation work correctly, ensure the following steps:
# 1. the MCP tool logic lays in 'src/climateclaw/tools/plugin_code_search/server.py':
# for isolated testing, comment out everything after the `detect_plugin_project` is called,
# and replace with the following: `return f"plugin_name: {plugin}, project_name: {project}"`.
# 2. then, spin up the docker compose environment with all necessary services (dev.sh script)
# 3. run this evaluation script from the terminal: `uv run scripts/dev_evaluate_tool.py`
#######################################################

MODEL = "gpt-4.1"  # model to benchmark
USER_ID = "janedoe"
RUNS = 1  # number of runs to perform -> increase for more thorough benchmarking
CONCURRENCY = 1  # number of concurrent runs to perform -> increase for more parallelism (aka faster execution)

PRINT_RUN_SUMMARY = (
    True  # print summary of each run (state, prompt, tool call, tool output)
)
SILENCE_LOGGING = True

# run evaluation of tool calls (TP, FP, FN, accuracy, precision, recall)
EVAL_TOOL = False
PLOT_METRICS = False  # plot metrics as bar charts at the end of the benchmark

## evaluation suite: direct questions about a plugin + indirect ones (more widely phrased)
path_to_prompts = (
    Path(__file__).parent / "evaluation" / "benchmark-prompts-plugin-server.json"
)
with open(path_to_prompts, "r", encoding="utf-8") as f:
    BENCHMARK = {k.lower(): v for k, v in json.load(f).items()}

# for quick sanity checking, use this one and comment out the main benchmark suite
# BENCHMARK = {
#     "leadtimeselektor": [
#         "How does the 'leadtimeselektor' plugin work from a high-level perspective?",
#         "How can I best extract lead times from decadal climate predictions?",
#         "How are you?",
#     ]
# }

path_to_plugins = (
    Path(__file__).parents[1]
    / "src/climateclaw/tools/plugin_code_search/available_plugins.md"
)
plugin_overview = path_to_plugins.read_text(encoding="utf-8")
plugins = re.findall(r"\*\*([^*]+)\*\*", plugin_overview)
ALL_PLUGINS = [p.lower() for p in plugins]

# ──────────────────────────────────────────────────────────────────────────────
log = configure_logging("plugin-code-server_eval-script")


@dataclass
class RunResult:
    idx: int
    thread_id: str
    state: str
    duration: float
    tool_name: str
    tool_args: dict[str, str]
    tool_output: str


async def _run_once(idx: int, sem: asyncio.Semaphore, prompt: str) -> RunResult:
    async with sem:
        thread_id = await new_thread_id()
        read_history = False

        Auth = Authenticator.local(username="dev_user")
        Storage = await ThreadStorage.create()
        create_dir_at_cache(thread_id)

        await prepare_for_stream(
            thread_id=thread_id,
            user_id=USER_ID,
            Auth=Auth,
            Storage=Storage,
            read_history=read_history,
        )

        system_prompt = get_entire_prompt(USER_ID, thread_id, MODEL)

        t0 = time.perf_counter()
        state = "Done"
        tool_name = ""
        tool_args = {}
        tool_output = ""

        try:
            async for variant in run_stream(
                model=MODEL,
                thread_id=thread_id,
                user_input=prompt,
                system_prompt=system_prompt,
                storage=Storage,
            ):
                if isinstance(variant, SVToolCall):
                    tool_name = variant.tool_name
                    tool_args = json.loads(variant.content or "{}")
                elif isinstance(variant, SVToolOutput):
                    tool_output = variant.content

            await end_and_save_conversation(thread_id, Storage)
        except Exception as e:
            state = f"Error:{e}"

        duration = time.perf_counter() - t0
        return RunResult(
            idx,
            thread_id,
            state,
            duration,
            tool_name,
            tool_args,
            tool_output,
        )


async def run_benchmark_for_prompt(
    prompt: str, plugin: str, eval_dict: dict[str, list[float]]
) -> dict[str, list[float]]:

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(_run_once(i, sem, prompt)) for i in range(RUNS)]
    results = await asyncio.gather(*tasks)
    assert results, "No results returned from benchmark runs"

    states = [r.state for r in results]
    durations = [r.duration for r in results]
    tool_names = [r.tool_name for r in results]
    tool_args = [r.tool_args for r in results]
    tool_outputs = [r.tool_output for r in results]

    if PRINT_RUN_SUMMARY:
        for state, duration, tool_name, tool_arg, tool_output in zip(
            states, durations, tool_names, tool_args, tool_outputs
        ):
            print(f"state: {state},\t Duration: {duration:.2f}s")
            print(f"Prompt: {prompt}")
            print(f"called tool: {tool_name}")
            print(f"  tool args: {tool_arg}")
            print(f"  tool output: {tool_output}\n")

    if EVAL_TOOL:
        metrics_dict = _evaluate_tool_call_results(
            tool_names, tool_args, tool_outputs, plugin
        )
        for k, v in metrics_dict.items():
            eval_dict[k].append(v)

    return eval_dict


def _evaluate_tool_call_results(
    tool_names: list[str],
    tool_args: list[dict[str, str]],
    tool_outputs: list[str],
    plugin: str,
) -> dict[str, float]:
    ## evaluate results w.r.t. whether the plugin tool was called at all by the model
    true_pos_tool = sum(
        [
            1
            for tc, ta in zip(tool_names, tool_args)
            if tc == "plugin_code_search" and "user_query" in ta
        ]
    )
    false_pos_tool = sum(
        [1 for tc in tool_names if tc != "plugin_code_search" and tc]
    )  # model called a plugin, but it was the wrong one
    false_neg_tool = sum(
        [1 for tc in tool_names if not tc]
    )  # model did not call any plugin, but should have done so

    ## evaluate results w.r.t. whether the correct plugin was selected by the model
    remaining_plugins = [p for p in ALL_PLUGINS if p != plugin]
    correct_tool_calls = [
        True if tc == "plugin_code_search" else False for tc in tool_names
    ]
    true_pos_plugin = sum(
        [1 for to, tc in zip(tool_outputs, correct_tool_calls) if plugin in to and tc]
    )  # model called the correct plugin and got the correct output
    false_pos_plugin = sum(
        [
            1
            for to, tc in zip(tool_outputs, correct_tool_calls)
            if any([p in to for p in remaining_plugins]) and tc
        ]
    )  # model called a plugin, but it was the wrong one
    false_neg_plugin = sum(
        [
            1
            for to, tc in zip(tool_outputs, correct_tool_calls)
            if not any([p in to for p in ALL_PLUGINS]) and tc
        ]
    )  # model did not call any known plugin, but should have done so

    # compute metrics
    accuracy_tool = true_pos_tool / RUNS
    precision_tool = (
        true_pos_tool / (true_pos_tool + false_pos_tool)
        if (true_pos_tool + false_pos_tool) > 0
        else 0
    )
    recall_tool = (
        true_pos_tool / (true_pos_tool + false_neg_tool)
        if (true_pos_tool + false_neg_tool) > 0
        else 0
    )
    accuracy_plugin = true_pos_plugin / RUNS
    precision_plugin = (
        true_pos_plugin / (true_pos_plugin + false_pos_plugin)
        if (true_pos_plugin + false_pos_plugin) > 0
        else 0
    )
    recall_plugin = (
        true_pos_plugin / (true_pos_plugin + false_neg_plugin)
        if (true_pos_plugin + false_neg_plugin) > 0
        else 0
    )

    return {
        "true_pos_tool": true_pos_tool / RUNS,
        "false_pos_tool": false_pos_tool / RUNS,
        "false_neg_tool": false_neg_tool / RUNS,
        "true_pos_plugin": true_pos_plugin / RUNS,
        "false_pos_plugin": false_pos_plugin / RUNS,
        "false_neg_plugin": false_neg_plugin / RUNS,
        "accuracy_tool": accuracy_tool,
        "precision_tool": precision_tool,
        "recall_tool": recall_tool,
        "accuracy_plugin": accuracy_plugin,
        "precision_plugin": precision_plugin,
        "recall_plugin": recall_plugin,
    }


def plot_metrics(avg_metrics: dict[str, float], save_dir: Path) -> None:
    current_date = datetime.today().strftime("%Y-%m-%d")
    # pie chart for tool/plugin TP, FP, FN
    fig, axs = plt.subplots(1, 2, figsize=(12, 6))
    metrics = ["TP", "FP", "FN"]
    tool_metrics = {
        "True Positives": avg_metrics.get("true_pos_tool", 0),
        "False Positives": avg_metrics.get("false_pos_tool", 0),
        "False Negatives": avg_metrics.get("false_neg_tool", 0),
    }
    plugin_metrics = {
        "True Positives": avg_metrics.get("true_pos_plugin", 0),
        "False Positives": avg_metrics.get("false_pos_plugin", 0),
        "False Negatives": avg_metrics.get("false_neg_plugin", 0),
    }
    charts = [tool_metrics, plugin_metrics]
    titles = ["Tool Call", "Plugin Call"]
    for ax, chart, title in zip(axs, charts, titles):
        pie = ax.pie(
            chart.values(),
            autopct="%1.1f%%",
            textprops=dict(fontsize=14, color="white", fontweight="bold"),
        )
        ax.legend(pie.wedges, metrics, title="Metrics", loc="best", fontsize=14)
        ax.set_title(title, fontsize=16)
    plt.suptitle(
        f"Evaluation Metrics for 'plugin_code_search' tool (Model: {MODEL}, Runs/Query: 30)"
    )
    plt.tight_layout()
    plt.subplots_adjust(wspace=0.4)
    plt.savefig(save_dir / f"tool_evaluation_pie_charts_{current_date}.png", dpi=400)

    # bar chart for tool/plugin accuracy, precision, recall
    fig, axs = plt.subplots(1, 2, figsize=(12, 6))
    for ax in axs:
        ax.set_ylim(0, 1)
        ax.set_ylabel("Metric Value", labelpad=12)
    tool_metrics = {
        "Accuracy": avg_metrics.get("accuracy_tool", 0),
        "Precision": avg_metrics.get("precision_tool", 0),
        "Recall": avg_metrics.get("recall_tool", 0),
    }
    plugin_metrics = {
        "Accuracy": avg_metrics.get("accuracy_plugin", 0),
        "Precision": avg_metrics.get("precision_plugin", 0),
        "Recall": avg_metrics.get("recall_plugin", 0),
    }
    axs[0].bar(tool_metrics.keys(), tool_metrics.values())
    axs[0].set_title("Tool Metrics")
    axs[1].bar(plugin_metrics.keys(), plugin_metrics.values())
    axs[1].set_title("Plugin Metrics")
    plt.suptitle(
        f"Evaluation Metrics for 'plugin_code_search' tool (Model: {MODEL}, Runs/Query: 30)"
    )
    plt.tight_layout()
    plt.subplots_adjust(wspace=0.3)
    plt.savefig(save_dir / f"tool_evaluation_metrics_{current_date}.png", dpi=400)


async def main() -> None:
    if SILENCE_LOGGING:
        silence_logger()

    eval_dict = defaultdict(list)
    prompt_count = sum(len(prompts) * RUNS for prompts in BENCHMARK.values())
    with tqdm(total=prompt_count, desc="Evaluation", unit="prompt") as progress:
        for plugin, prompts in BENCHMARK.items():
            progress.set_postfix_str(f"plugin={plugin}")
            for prompt in prompts:
                # print(f"\n=== Running benchmark for prompt: {prompt} ===")
                eval_dict = await run_benchmark_for_prompt(prompt, plugin, eval_dict)
                progress.update(RUNS)

    # compute average metrics across all prompts
    if EVAL_TOOL:
        avg_metrics = {k: sum(v) / len(v) for k, v in eval_dict.items()}
        print(
            f"\n=== Average metrics [in %] across all prompts ({RUNS} runs/prompt) ==="
        )
        for k, v in avg_metrics.items():
            print(f"  {k:<17}: {v:.1%}")

    # plot metrics as bar charts (one for tool, one for plugin)
    if PLOT_METRICS and EVAL_TOOL:
        eval_dir = Path(__file__).parent / "evaluation"
        eval_dir.mkdir(parents=True, exist_ok=True)
        plot_metrics(avg_metrics, eval_dir)


if __name__ == "__main__":
    asyncio.run(main())
