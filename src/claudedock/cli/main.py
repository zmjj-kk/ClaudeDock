from __future__ import annotations

import argparse
import sys

from claudedock.cli.commands.chat import cmd_chat
from claudedock.cli.commands.core import cmd_core_start, cmd_core_status, cmd_core_stop
from claudedock.cli.commands.memory import cmd_memory_consolidate
from claudedock.cli.commands.ping import cmd_ping
from claudedock.cli.commands.run import cmd_run
from claudedock.cli.commands.trace import cmd_trace
from claudedock.cli.commands.version import cmd_version
from claudedock.core.config import get_config
from claudedock.core.logging_setup import setup_logging


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="claudedock", description="ClaudeDock CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")
    subparsers.add_parser("chat", help="Start a multi-turn chat session")

    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    core_parser = subparsers.add_parser("core", help="Manage the core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_sub.add_parser("start", help="Start the daemon in the background")
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("status", help="Show daemon status")

    memory_parser = subparsers.add_parser("memory", help="Manage long-term memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    memory_sub.add_parser(
        "consolidate", help="Deduplicate, resolve conflicts, and abstract memories"
    )

    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    eval_parser = subparsers.add_parser(
        "eval-rag", help="Evaluate RAG retrieval & generation quality"
    )
    eval_parser.add_argument("--dataset", default="docs/evaluation/rag_queries.jsonl")
    eval_parser.add_argument("--layer", choices=["retrieval", "generation", "both"], default="both")
    eval_parser.add_argument(
        "--backend", choices=["bm25", "embedding", "hybrid", "all"], default="all"
    )
    eval_parser.add_argument("--top-k", type=int, default=None)
    eval_parser.add_argument("--output", default=None)
    eval_parser.add_argument(
        "--rerank", action="store_true", help="检索层对比 baseline 与 +rerank 精排"
    )
    eval_parser.add_argument(
        "--filter-check", action="store_true", help="检索层额外计算元数据过滤合规率"
    )
    eval_parser.add_argument(
        "--query-enhance",
        action="store_true",
        help="检索层对比 baseline 与 +query增强(改写+拓展) 的召回差异",
    )

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config()
    setup_logging(config)

    if args.command == "ping":
        cmd_ping(config)
    elif args.command == "chat":
        cmd_chat(config)
    elif args.command == "run":
        cmd_run(args.goal, config)
    elif args.command == "core":
        if args.core_command == "start":
            cmd_core_start(config)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "status":
            cmd_core_status(config)
        else:
            core_parser.print_help()
            sys.exit(1)
    elif args.command == "memory":
        if args.memory_command == "consolidate":
            cmd_memory_consolidate(config)
        else:
            memory_parser.print_help()
            sys.exit(1)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    elif args.command == "eval-rag":
        from claudedock.cli.commands.eval_rag import cmd_eval_rag

        cmd_eval_rag(args, config)
    else:
        parser.print_help()
        sys.exit(1)
