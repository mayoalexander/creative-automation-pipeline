"""The `creative-kit` command (short: `ckit`; also `python -m engines.vanilla`): the CLI door onto ops.py.

    creative-kit                                   what it can do  (short: ckit)
    creative-kit explain spring-launch jp          which config file set each value
    creative-kit run spring-launch --provider mock
    creative-kit set markets/jp.yaml overrides.acme.marketing.cta 今すぐチェック --as yuki@example.com
    creative-kit studio | creative-kit mcp             the UI + API, and the MCP server

One command per operation, generated from ops.py: the natural arguments are typed bare, the rest
are flags, the output is for people (--json for scripts). Old flag forms (run --campaign X) still work.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from .config import load_config, load_env
from .access import Forbidden
from .explain import pad, print_explain, width
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


# ───────────────────────────────────────────────────────── output for people (--json gives the raw result)
def _cell(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else "" if v is None else str(v)


def table(rows: list[dict], cols: list[str]) -> None:
    widths = {c: min(max([len(c)] + [width(_cell(r.get(c))) for r in rows]), 44) + 2 for c in cols}
    print("".join(pad(c.upper(), widths[c]) for c in cols).rstrip())
    for r in rows:
        print("".join(pad(_cell(r.get(c)), widths[c]) for c in cols).rstrip())


def show(name: str, result, args) -> int:
    """Print an operation's result for a person. Returns the exit code (3 = not allowed)."""
    if name == "campaigns":
        table(result, ["id", "name", "brand", "partner", "markets", "products", "creative_types"])
    elif name == "explain":
        for e in result if isinstance(result, list) else [result]:
            print_explain(e)
    elif name == "report":
        print_report(result)
    elif name == "deliverables":
        table(result, ["market", "product", "creative", "channel", "format", "status", "path"])
        print(f"\n{len(result)} deliverables · {sum(i['status'] == 'built' for i in result)} built")
    elif name == "who":
        where = result["path"] + (f"#{result['section']}" if result["section"] else "")
        print(f"who may {result['action']} {where}" + (" (locked section)" if result["protected"] else ""))
        for team, members in result["teams"].items():
            print(f"  {team:<16}{', '.join(members)}")
    elif name == "can":
        where = result["path"] + (f"#{result['section']}" if result["section"] else "")
        print(f"{result['user']} {'may' if result['allowed'] else 'may NOT'} {result['action']} {where}")
        return 0 if result["allowed"] else 3
    elif name == "config_get":
        teams = lambda m: ", ".join(m) or "nobody"
        print(f"# {result['file']}  ·  edit: {teams(result['editors'])}  ·  delete: {teams(result['deleters'])}")
        for lock in result["protected"]:
            print(f"# locked: {lock['section']}  ·  only {teams(lock['editors'])}")
        print((Path(args.config) / result["file"]).read_text().rstrip())
    elif name == "config_set":
        print(f"updated {result['path']}: {', '.join(result['changed'])}  (as {result['user']}, audited)"
              if result["changed"] else f"no change to {result['path']}")
    elif name == "run":
        report = json.loads((Path(result["output"]) / "report.json").read_text())
        print_report(report)
        t = report["totals"]
        return 1 if (t["errors"] or t["built"] and t["brand_checks_passed"] < t["built"]) else 0
    elif name == "snapshot":
        print(f"{len(result['campaigns'])} campaigns · {len(result['markets'])} markets · {len(result['deliverables'])} deliverables"
              f" · {sum(len(v) for k, v in result['config'].items() if isinstance(v, dict))} config files  (--json for all of it)")
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def show_help(h: dict) -> None:
    names = h.get("cli_commands", {})
    if "operations" in h:
        print("creative-kit: " + h["about"] + "\n\nActing as: " + h["acting_as"])
        for kind, ops in h["operations"].items():
            print(f"\n{kind.upper()}")
            for name, summary in ops.items():
                print(f"  {names.get(name, name):<14}{summary}")
        print("\nSERVE IT\n  studio        the UI + API on http://localhost:8765\n  mcp           every command as a tool for Claude and other agents")
        print("\nOPTIONS (any command)\n  --as PERSON   who is acting (default $PIPELINE_USER, else you, locally)\n  --json        the raw result, for scripts\n"
              "  --config DIR  --out DIR  --env-file FILE")
        print("\nStart with: creative-kit campaigns · creative-kit explain spring-launch jp · creative-kit help <command>")
        return
    print(f"creative-kit {h['cli']}  ({'writes' if h['writes'] else 'read-only'})\n\n{h['description']}\n\nARGUMENTS")
    from .ops import OPS, positional
    bare = positional(OPS[h["name"]])
    for k, a in h["arguments"].items():
        label = k.upper() if k in bare else f"--{k.replace('_', '-')}"
        print(f"  {label:<16}{a['type']:<6}" + ("required" if a["required"] else f"default {a['default']!r}"))
    if not h["arguments"]:
        print("  none")
    if h.get("value"):
        print(f"\n  VALUE is {h['value']}")
    if h.get("needs"):
        print(f"\nNEEDS\n  {h['needs']}")
    for i, ex in enumerate(h["examples"], 1):
        print(f"\nEXAMPLE {i}" if len(h["examples"]) > 1 else "\nEXAMPLE")
        print(f"  CLI  {ex['cli']}\n  API  {ex['api']}\n  MCP  {ex['mcp']}")


# ───────────────────────────────────────────────────────── the command line
def _common() -> argparse.ArgumentParser:
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--as", dest="user", help="who is acting (default $PIPELINE_USER, else you, locally)")
    c.add_argument("--json", action="store_true", help="print the raw result, for scripts")
    c.add_argument("--config", default="config")
    c.add_argument("--out", default="output")
    c.add_argument("--env-file", default=".env", help="where API keys come from (default .env)")
    c.add_argument("-v", "--verbose", action="store_true")
    return c


def parser() -> argparse.ArgumentParser:
    from .ops import OPS, positional
    common = _common()
    ap = argparse.ArgumentParser(prog="creative-kit", description="Creative automation pipeline. `creative-kit help` for every command.",
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")
    for o in OPS.values():
        bare = positional(o)
        q = sub.add_parser(o.cli, parents=[common], help=o.summary, description=__import__("inspect").getdoc(o.fn),
                           usage=f"creative-kit {o.cli} " + " ".join(n.upper() if n in [p.name for p in o.params() if p.default is p.empty] else f"[{n.upper()}]" for n in bare)
                                 + (" [options]" if len(bare) < len(o.params()) else ""))
        if bare:
            q.add_argument("bare", nargs="*", metavar=" ".join(n.upper() for n in bare), help=argparse.SUPPRESS)
        for prm in o.params():                        # every argument also works as a flag: run --campaign X
            flag = "--" + prm.name.replace("_", "-")
            if prm.annotation in (bool, "bool"):
                q.add_argument(flag, dest=prm.name, action="store_true")
            else:
                q.add_argument(flag, dest=prm.name, default=None, help=None if prm.name not in bare else argparse.SUPPRESS)
        if o.name == "explain":
            q.add_argument("--all", action="store_true", help=argparse.SUPPRESS)   # old form; no args already means all
        if o.name == "run":
            q.set_defaults(provider_choices=("openai", "openrouter", "mock"))
    so = sub.add_parser("studio", parents=[common], help="the UI + API on localhost (POST /api/<operation>)")
    so.add_argument("--port", type=int, default=8765)
    mc = sub.add_parser("mcp", parents=[common], help="every command as an MCP tool, for Claude and other agents")
    mc.add_argument("--http", action="store_true", help="streamable HTTP with a bearer token instead of stdio")
    mc.add_argument("--port", type=int, default=3210)
    mc.add_argument("--token", help="bearer token for --http (generated and printed if omitted)")
    mc.add_argument("--host", default="127.0.0.1", help="for --http: 0.0.0.0 in a container or cloud host")
    return ap


def main(argv: list[str] | None = None) -> int:
    from .ops import OPS, coerce, context_from_env, positional
    args = parser().parse_args(argv)
    # locally, the person at the keyboard is acting; MCP and the API never default to anyone
    user = args.user or os.environ.get("PIPELINE_USER") or f"{getpass.getuser()} (local)"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd in (None, "help") and not getattr(args, "bare", None) and not getattr(args, "operation", None):
        h = OPS["help"](context_from_env(args.config, args.out, user))
        print(json.dumps(h, indent=2, ensure_ascii=False)) if args.json else show_help(h)
        return 0
    load_env(args.env_file)
    if args.cmd == "studio":
        from .studio import serve
        serve(args.config, args.out, args.port)
        return 0
    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(context_from_env(args.config, args.out, args.user), args.http, args.port, args.token, args.host)
        return 0

    o = next(x for x in OPS.values() if x.cli == args.cmd)
    kw = {p.name: getattr(args, p.name) for p in o.params() if getattr(args, p.name) not in (None, False)}
    bare = list(getattr(args, "bare", []) or [])
    for name in positional(o):                       # bare words fill the natural arguments in order
        if bare and name not in kw:
            kw[name] = bare.pop(0)
    if bare:
        raise SystemExit(f"creative-kit {o.cli}: too many arguments: {' '.join(bare)}  (creative-kit help {o.cli})")
    missing = [p.name for p in o.params() if p.default is p.empty and p.name not in kw]
    if missing:
        raise SystemExit(f"creative-kit {o.cli}: missing {', '.join(n.upper() for n in missing)}  (creative-kit help {o.cli})")
    if "value" in kw:
        kw["value"] = coerce(kw["value"])
    if o.name == "run" and kw.get("provider") and kw["provider"] not in args.provider_choices:
        raise SystemExit(f"creative-kit run: --provider must be one of {', '.join(args.provider_choices)}")
    try:
        result = o(context_from_env(args.config, args.out, user), **kw)
    except Forbidden as e:
        print(f"refused: {e}" + ("  (use --as <a team member>)" if "(local)" in user else ""), file=sys.stderr)
        return 3
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"error: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 2
    if o.name == "help":
        print(json.dumps(result, indent=2, ensure_ascii=False)) if args.json else show_help(result)
        return 0
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    return show(o.name, result, args)


if __name__ == "__main__":
    sys.exit(main())
