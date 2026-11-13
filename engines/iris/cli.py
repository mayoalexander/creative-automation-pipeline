"""Command line entry point for the IRIS engine.

    python -m engines.iris run --campaign spring-launch                 # dry run: prints IRIS commands
    python -m engines.iris run --campaign spring-launch --publish       # writes to your IRIS workspace
    python -m engines.iris sync-approvals --campaign spring-launch      # pull approver ticks back
"""
from __future__ import annotations

import argparse
import logging
import sys

from engines.vanilla.config import load_env
from engines.vanilla.providers import ProviderError
from engines.vanilla.report import print_report

from .client import IrisError
from .engine import run, sync_approvals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engines.iris")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--campaign", required=True)
    r.add_argument("--config", default="config")
    r.add_argument("--out", default="output")
    r.add_argument("--provider", choices=["openai", "openrouter", "mock"])
    r.add_argument("--markets")
    r.add_argument("--publish", action="store_true", help="write to IRIS (default: dry run)")
    r.add_argument("--agent", help="IRIS agent id to review localized copy")
    s = sub.add_parser("sync-approvals")
    s.add_argument("--campaign", required=True)
    s.add_argument("--out", default="output")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env()
    try:
        if args.cmd == "sync-approvals":
            for market, decision in sync_approvals(args.out, args.campaign).items():
                print(f" approval  {market:<4} {decision}")
            return 0
        report = run(args.config, args.campaign, args.out, args.provider, args.publish,
                     args.markets.split(",") if args.markets else None, args.agent)
    except (IrisError, ProviderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_report(report)
    info = report["iris"]
    print(f" IRIS {info['mode']}: {len(info['commands'])} commands, {info['records']} dataset records")
    if info["mode"] == "publish":
        print(f" workspace: https://heyiris.io/atlas?project={info['state']['bloq_id']}")
    else:
        print(f" see output/iris/{args.campaign}/iris_commands.sh for exactly what --publish would run")
    for market, verdict in (report.get("agent_review") or {}).items():
        print(f" agent review {market}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
