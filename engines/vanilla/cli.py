"""Command line entry point.

    python -m engines.vanilla run --campaign spring-launch [--provider mock|openai|openrouter]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import yaml

from .config import load_config, load_env
from .access import Access, ConfigStore, Forbidden
from .explain import explain_market, print_explain
from .pipeline import render_phase, resolve_phase, source_phase, write_market_files
from .providers import ProviderError, build_providers
from .report import build_report, print_report, write_outputs

ENGINE = "vanilla"


def run(config_dir: str, campaign_id: str, out_root: str, provider: str | None,
        markets: list[str] | None = None, plan_only: bool = False, engine: str = ENGINE) -> dict:
    t0 = time.time()
    cfg = load_config(config_dir)
    if campaign_id not in cfg.campaigns:
        raise SystemExit(f"unknown campaign '{campaign_id}'. Available: {', '.join(cfg.campaigns)}")
    campaign = cfg.campaigns[campaign_id]
    genai = cfg.org.get("creative_resources", {}).get("genai", {})
    kind = provider or genai.get("image_provider", "mock")
    image_provider, translator = build_providers(kind, genai)

    out = Path(out_root) / engine / campaign_id
    out.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(out / "run.log", mode="w")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    selected = markets or campaign["markets"]
    plans = resolve_phase(cfg, campaign, selected, translator)
    sources = {} if plan_only else source_phase(cfg.root, campaign, plans, image_provider)
    items = render_phase(cfg, campaign, plans, sources, out) if not plan_only else []
    write_market_files(plans, out)
    report = build_report(engine, cfg, campaign, plans, sources, items, time.time() - t0, image_provider.name)
    write_outputs(out, report, items)
    logging.getLogger().removeHandler(fh)
    report["_items"] = items  # in-memory only, for engines that build on this one
    return report


def explain_cmd(args) -> int:
    cfg = load_config(args.config)
    if args.all:
        pairs = [(c, m) for c in cfg.campaigns.values() for m in c["markets"]]
    elif args.campaign and args.market:
        pairs = [(cfg.campaigns[args.campaign], args.market)]
    else:
        raise SystemExit("use --campaign X --market Y, or --all")
    results = [explain_market(cfg, c, m) for c, m in pairs]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print_explain(r)
    return 0


def access_cmd(args) -> int:
    access = Access.load(args.config)
    where = f"{args.path}#{args.section}" if args.section else args.path
    if args.set is not None or args.delete:
        if not args.user:
            raise SystemExit("--as is required to change anything")
        store = ConfigStore(args.config, access)
        try:
            if args.delete:
                store.delete(args.user, args.path)
                print(f"deleted {args.path}")
            else:
                changed = store.set(args.user, args.path, args.section, yaml.safe_load(args.set))
                print(f"updated {args.path}: {', '.join(changed) or 'no change'}")
        except Forbidden as e:
            print(f"refused: {e}", file=sys.stderr)
            return 3
        return 0
    if args.user:
        ok = access.can(args.user, args.action, args.path, args.section)
        print(f"{args.user} {'may' if ok else 'may NOT'} {args.action} {where}")
        return 0 if ok else 3
    owners = access.who(args.action, args.path, args.section)
    print(f"who may {args.action} {where}" + (" (protected section)" if access.is_protected(args.section) else ""))
    for team, members in owners.items():
        print(f"  {team:<16}{', '.join(members)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engines.vanilla", description="Creative automation pipeline (vanilla)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a campaign")
    r.add_argument("--campaign", required=True)
    r.add_argument("--config", default="config")
    r.add_argument("--out", default="output")
    r.add_argument("--env-file", default=".env", help="where to read API keys from (default .env)")
    r.add_argument("--provider", choices=["openai", "openrouter", "mock"],
                   help="overrides creative_resources.genai.image_provider in org.yaml")
    r.add_argument("--markets", help="comma-separated subset, e.g. jp,mx")
    r.add_argument("--plan-only", action="store_true", help="resolve + legal check only, render nothing")
    r.add_argument("-v", "--verbose", action="store_true")
    ac = sub.add_parser("access", help="who may do what to a config file (and edit it, checked)")
    ac.add_argument("path", help="config path, e.g. markets/jp.yaml")
    ac.add_argument("--section", help="dotted section, e.g. overrides.acme.legal")
    ac.add_argument("--action", default="update", choices=["read", "create", "update", "delete"])
    ac.add_argument("--as", dest="user", help="check (or act) as this person")
    ac.add_argument("--set", help="with --as and --section: write this YAML value")
    ac.add_argument("--delete", action="store_true", help="with --as: delete the file")
    ac.add_argument("--config", default="config")
    x = sub.add_parser("explain", help="show which config file set each value for a market")
    x.add_argument("--campaign")
    x.add_argument("--market")
    x.add_argument("--all", action="store_true", help="every campaign × market")
    x.add_argument("--config", default="config")
    x.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "explain":
        return explain_cmd(args)
    if args.cmd == "access":
        return access_cmd(args)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    load_env(getattr(args, "env_file", ".env"))
    try:
        report = run(args.config, args.campaign, args.out, args.provider,
                     args.markets.split(",") if args.markets else None, args.plan_only)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_report(report)
    t = report["totals"]
    return 1 if (t["errors"] or t["built"] and t["brand_checks_passed"] < t["built"]) else 0


if __name__ == "__main__":
    sys.exit(main())
