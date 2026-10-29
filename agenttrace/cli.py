"""Command line. `run` executes and serves nothing by itself; Make chains serve."""

from __future__ import annotations

import argparse

from agenttrace.service import record_command, run_command, serve_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenttrace", description="Trace a warehouse-triage agent.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Replay the dataset, print SLOs, write runs/latest")
    sub.add_parser("record", help="Regenerate demo/data and site/data from the mock provider")
    serve = sub.add_parser("serve", help="Serve site/ on 127.0.0.1")
    serve.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command()
    if args.command == "record":
        return record_command()
    if args.command == "serve":
        return serve_command(args.port)
    return 2
