"""Command line entry point.

    python -m engines.vanilla run --campaign spring-launch [--provider mock|openai|openrouter]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import load_config, load_env
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engines.vanilla", description="Creative automation pipeline (vanilla)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a campaign")
    r.add_argument("--campaign", required=True)
    r.add_argument("--config", default="config")
    r.add_argument("--out", default="output")
    r.add_argument("--provider", choices=["openai", "openrouter", "mock"],
                   help="overrides creative_resources.genai.image_provider in org.yaml")
    r.add_argument("--markets", help="comma-separated subset, e.g. jp,mx")
    r.add_argument("--plan-only", action="store_true", help="resolve + legal check only, render nothing")
    r.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    load_env()
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
