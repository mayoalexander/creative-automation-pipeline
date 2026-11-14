"""Command line entry point for the LangGraph engine.

    python -m engines.langgraph run --campaign spring-launch [--provider mock] [--auto-approve]
    python -m engines.langgraph graph            # print the graph as Mermaid
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from engines.vanilla.config import load_env
from engines.vanilla.providers import ProviderError
from engines.vanilla.report import print_report

from .graph import build_graph


def run(config_dir: str, campaign_id: str, out_root: str, provider: str | None,
        markets: list[str] | None = None, auto_approve: bool = True, decide=None) -> dict:
    """Run to completion. `decide(payload) -> 'approved'|'rejected'` answers each interrupt."""
    from engines.vanilla.config import load_config
    kind = provider or load_config(config_dir).org["creative_resources"]["genai"].get("image_provider", "mock")
    graph = build_graph(checkpointer=InMemorySaver())
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = {"config_dir": config_dir, "campaign_id": campaign_id, "out_root": out_root,
             "provider": kind, "markets": markets or [], "auto_approve": auto_approve}
    result = graph.invoke(state, thread)
    while "__interrupt__" in result:                    # paused at the approval gate
        payload = result["__interrupt__"][0].value
        answer = decide(payload) if decide else "approved"
        result = graph.invoke(Command(resume=answer), thread)
    return result["report"]


def ask_in_terminal(payload: dict) -> str:
    print(f"\n── approval needed: {payload['market']} ── approvers: {', '.join(payload['approvers'])}")
    print(f"   message: {payload['message']}")
    for c in payload["creatives"][:6]:
        print(f"   · {c}")
    ans = input("   approve? [y/N] ").strip().lower()
    return "approved" if ans in ("y", "yes") else "rejected"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engines.langgraph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--campaign", required=True)
    r.add_argument("--config", default="config")
    r.add_argument("--out", default="output")
    r.add_argument("--env-file", default=".env", help="where to read API keys from (default .env)")
    r.add_argument("--provider", choices=["openai", "openrouter", "mock"])
    r.add_argument("--markets")
    r.add_argument("--auto-approve", action="store_true", help="skip the human approval gate")
    sub.add_parser("graph", help="print the pipeline graph as Mermaid")
    args = ap.parse_args(argv)

    if args.cmd == "graph":
        print(build_graph().get_graph().draw_mermaid())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env(getattr(args, "env_file", ".env"))
    try:
        report = run(args.config, args.campaign, args.out, args.provider,
                     args.markets.split(",") if args.markets else None,
                     auto_approve=args.auto_approve, decide=ask_in_terminal)
    except ValidationError as e:
        print(f"config error:\n{e}", file=sys.stderr)
        return 2
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_report(report)
    for a in report.get("approvals", []):
        print(f" approval  {a['market']:<4} {a['decision']}")
    t = report["totals"]
    return 1 if t["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
