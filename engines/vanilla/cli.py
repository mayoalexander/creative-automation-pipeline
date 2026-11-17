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
    sources = {} if plan_only else source_phase(cfg, campaign, plans, image_provider)
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


def help_cmd(args) -> int:
    """The help operation, printed for people (--json prints what the API and MCP get)."""
    from .ops import OPS, context_from_env
    try:
        h = OPS["help"](context_from_env(user=args.user), operation=args.operation)
    except KeyError as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(h, indent=2, ensure_ascii=False))
        return 0
    if not args.operation:
        print(h["about"] + "\n\nActing as: " + h["acting_as"])
        for kind, ops in h["operations"].items():
            print(f"\n{kind.upper()}")
            for name, summary in ops.items():
                print(f"  {name:<14}{summary}")
        print("\nINTERFACES")
        for k, v in h["interfaces"].items():
            print(f"  {k.upper():<5}{v}")
        print("\nStart with: " + ", ".join(h["start_here"]) + ".  Details: python -m engines.vanilla help <operation>")
        return 0
    print(f"{h['name']}  ({'writes' if h['writes'] else 'read-only'})\n\n{h['description']}\n\nARGUMENTS")
    for k, a in h["arguments"].items():
        print(f"  --{k.replace('_', '-'):<14}{a['type']:<6}" + ("required" if a["required"] else f"default {a['default']!r}"))
    if not h["arguments"]:
        print("  none")
    if h.get("needs"):
        print(f"\nNEEDS\n  {h['needs']}")
    for i, ex in enumerate(h["examples"], 1):
        print(f"\nEXAMPLE {i}" if len(h["examples"]) > 1 else "\nEXAMPLE")
        print(f"  CLI  {ex['cli']}\n  API  {ex['api']}\n  MCP  {ex['mcp']}")
    return 0


def interface_cmd(args) -> int:
    """UI + API (studio), MCP (mcp) and the generic CLI (op): three doors onto the same ops.py."""
    from .ops import OPS, Context, coerce, context_from_env, describe
    load_env(args.env_file)
    if args.cmd == "studio":
        from .studio import serve
        serve(args.config, args.out, args.port)
        return 0
    ctx = context_from_env(args.config, args.out, args.user)
    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(ctx, args.http, args.port, args.token, args.host)
        return 0
    if args.list or not args.op:
        for o in describe():
            flags = " ".join(("--" if p["required"] else "[--") + k.replace("_", "-") + ("" if p["required"] else "]")
                             for k, p in o["params"].items())
            print(f"  {'write' if o['write'] else 'read ':<6}{o['name']:<14}{flags}\n{'':20}{o['summary']}")
        return 0
    spec = OPS[args.op]
    kw = {p.name: getattr(args, p.name) for p in spec.params()}
    kw = {k: coerce(v) if isinstance(v, str) and k == "value" else v for k, v in kw.items()}
    try:
        print(json.dumps(spec(ctx, **kw), indent=2, ensure_ascii=False, default=str))
    except Forbidden as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
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
    so = sub.add_parser("studio", help="UI + API: the Studio on localhost, and POST /api/<op> for every operation")
    so.add_argument("--port", type=int, default=8765)
    mc = sub.add_parser("mcp", help="MCP: every operation as a tool for Claude and other agents")
    mc.add_argument("--http", action="store_true", help="streamable HTTP with a bearer token instead of stdio")
    mc.add_argument("--port", type=int, default=3210)
    mc.add_argument("--token", help="bearer token for --http (generated and printed if omitted)")
    mc.add_argument("--host", default="127.0.0.1", help="for --http: 0.0.0.0 in a container or cloud host")
    mc.add_argument("--as", dest="user", help="act as this person (default: $PIPELINE_USER; none = read-only)")
    for p in (so, mc):
        p.add_argument("--config", default="config")
        p.add_argument("--out", default="output")
        p.add_argument("--env-file", default=".env", help="API keys for run (default .env)")
    hp = sub.add_parser("help", help="how to use it: every operation, or one with its arguments and examples")
    hp.add_argument("operation", nargs="?", default="")
    hp.add_argument("--as", dest="user")
    hp.add_argument("--json", action="store_true", help="the same document the API and MCP return")
    o = sub.add_parser("op", help="call any operation by name and print JSON (python -m engines.vanilla op --list)")
    o.add_argument("--list", action="store_true", help="every operation, its arguments and whether it writes")
    o.add_argument("--config", default="config")
    o.add_argument("--out", default="output")
    o.add_argument("--env-file", default=".env")
    o.add_argument("--as", dest="user", help="act as this person (default: $PIPELINE_USER)")
    osub = o.add_subparsers(dest="op")
    from .ops import OPS
    import inspect as _i
    for name, spec in OPS.items():                   # one subcommand per operation, flags from its signature
        q = osub.add_parser(name, help=spec.summary, description=_i.getdoc(spec.fn))
        for prm in spec.params():
            flag = "--" + prm.name.replace("_", "-")
            if prm.annotation in (bool, "bool"):
                q.add_argument(flag, dest=prm.name, action="store_true")
            else:
                q.add_argument(flag, dest=prm.name, required=prm.default is _i.Parameter.empty,
                               default=None if prm.default is _i.Parameter.empty else prm.default)
    args = ap.parse_args(argv)

    if args.cmd == "explain":
        return explain_cmd(args)
    if args.cmd == "access":
        return access_cmd(args)
    if args.cmd == "help":
        return help_cmd(args)
    if args.cmd in ("studio", "mcp", "op"):
        return interface_cmd(args)

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
