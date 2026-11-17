"""The pipeline's operations: the one layer every interface calls.

    UI (Studio) ──► API  /api/<op> ──┐
    CLI   creative-kit <command> ─────────────────┼──► OPS[<op>](ctx, **args) ──► config · access · engine
    MCP   one tool per op (Claude, Cursor, …) ──┘

Each operation is declared once below, with typed arguments and a docstring. The CLI flags, the API
routes and the MCP tools are generated from that declaration, so a new operation shows up in all of
them, and so do the rules: write operations need a person (`ctx.user`), and access.py checks them.
The person never comes from the operation's arguments: the CLI takes --as, the API the Studio's
picker (or a token when hosted), MCP the PIPELINE_USER the server was started with. An agent can't
choose who it is.
"""
from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from .access import Access, ConfigStore, Forbidden
from .config import load_config
from .explain import explain_market


@dataclass
class Context:
    config_dir: Path = Path("config")
    out_root: Path = Path("output")
    user: str | None = None             # who is acting; required for write operations


@dataclass
class Op:
    name: str
    fn: Callable[..., Any]
    write: bool
    summary: str
    examples: list[dict]
    cli: str                            # the command name in `creative-kit <cli>` (short for the config ops)

    def params(self) -> list[inspect.Parameter]:
        return list(inspect.signature(self.fn).parameters.values())[1:]      # without ctx

    def __call__(self, ctx: Context, **args) -> Any:
        if self.write and not ctx.user:
            raise Forbidden(f"'{self.name}' changes things, so it needs a person: --as, PIPELINE_USER or the Studio picker")
        return self.fn(ctx, **args)


OPS: dict[str, Op] = {}


def op(write: bool = False, examples: list[dict] | None = None, cli: str | None = None):
    def register(fn):
        OPS[fn.__name__] = Op(fn.__name__, fn, write, (fn.__doc__ or "").strip().splitlines()[0], examples or [{}],
                              cli or fn.__name__.replace("_", "-"))
        return fn
    return register


def describe() -> list[dict]:
    """The registry as data: what /api/ops returns and what the docs print."""
    return [{"name": o.name, "write": o.write, "summary": o.summary,
             "params": {p.name: {"type": str(p.annotation), "required": p.default is inspect.Parameter.empty,
                                 **({} if p.default is inspect.Parameter.empty else {"default": p.default})}
                        for p in o.params()}} for o in OPS.values()]


def _report(ctx: Context, campaign: str, name: str):
    f = Path(ctx.out_root) / "vanilla" / campaign / name
    if not f.exists():
        raise FileNotFoundError(f"no run of '{campaign}' yet: run it first")
    return json.loads(f.read_text())


def _validate(ctx: Context) -> None:
    """A config every campaign can still use: it loads, and every market of every campaign resolves."""
    cfg = load_config(ctx.config_dir)
    for c in cfg.campaigns.values():
        cfg.creative_types(c)
        for m in c["markets"]:
            explain_market(cfg, c, m)


# ─────────────────────────────────────────────────────────────── read
POSITIONAL = ("operation", "campaign", "market", "path", "section", "value")   # typed bare, in this order


def positional(o: Op) -> list[str]:
    """The arguments `creative-kit <cmd>` takes without a flag: the natural ones, in signature order."""
    return [p.name for p in o.params() if p.name in POSITIONAL]


def cli_example(o: Op, ex: dict) -> str:
    """`creative-kit <cmd> campaign market … [--flag value]`, quoted only where a shell needs it."""
    import re
    import shlex
    q = lambda v: (v if isinstance(v, str) and re.fullmatch(r"[\w@%+=:,./-]+", v) else
                   shlex.quote(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
    ex, words = dict(ex), ["creative-kit", o.cli]
    for name in positional(o):
        if name not in ex:
            break
        words.append(q(ex.pop(name)))
    for k, v in ex.items():
        words += [f"--{k.replace('_', '-')}"] + ([] if v is True else [q(v)])
    return " ".join(words + (["--as", "you@example.com"] if o.write else []))


def _usage(o: Op, ctx: Context) -> dict:
    return {
        "name": o.name, "cli": o.cli, "writes": o.write, "summary": o.summary, "description": inspect.getdoc(o.fn),
        "arguments": {p.name: {"type": {"Any": "yaml"}.get(str(p.annotation), getattr(p.annotation, "__name__", str(p.annotation))),
                               "required": p.default is inspect.Parameter.empty,
                               **({} if p.default is inspect.Parameter.empty else {"default": p.default})} for p in o.params()},
        "examples": [{"cli": cli_example(o, ex),
                      "api": f"curl -XPOST localhost:8765/api/{o.name}" + (" -H 'X-Acting-As: you@example.com'" if o.write else "")
                             + (f" -d '{json.dumps(ex, ensure_ascii=False)}'" if ex else ""),
                      "mcp": f"{o.name}({json.dumps(ex, ensure_ascii=False)})"} for ex in o.examples],
        **({"value": "YAML: text, a number, true/false or a list like [a, b]"} if "value" in inspect.signature(o.fn).parameters else {}),
        **({"needs": "a person acting (--as, X-Acting-As or PIPELINE_USER); checked against teams.yaml and audited"} if o.write else {}),
    }


@op(examples=[{}, {"operation": "set"}])
def help(ctx: Context, operation: str = "") -> dict:
    """How to use the pipeline: every operation, or one operation's arguments and examples (CLI, API and MCP)."""
    if operation:
        operation = {o.cli: o.name for o in OPS.values()}.get(operation, operation)      # `help set` = `help config_set`
        if operation not in OPS:
            raise KeyError(f"no operation '{operation}'. Operations: {', '.join(OPS)}")
        return _usage(OPS[operation], ctx)
    return {
        "about": "Creative automation pipeline: one campaign brief -> localized, on-brand ad creatives per market. "
                 "The same operations through four interfaces: UI (Studio), CLI, API and MCP.",
        "acting_as": ctx.user or "nobody: read-only (write operations need --as, X-Acting-As or PIPELINE_USER)",
        "start_here": ["campaigns", "explain", "report", "who"],
        "operations": {"read": {o.name: o.summary for o in OPS.values() if not o.write},
                       "write": {o.name: o.summary for o in OPS.values() if o.write}},
        "cli_commands": {o.name: o.cli for o in OPS.values()},
        "interfaces": {"cli": "creative-kit <command> ARGS   (creative-kit help <command>; --json for machines)",
                       "api": "POST /api/<operation> with a JSON body; GET /api/ops lists them   (creative-kit studio)",
                       "mcp": "one tool per operation   (creative-kit mcp, or .mcp.json in Claude Code)",
                       "ui": "the Studio at http://localhost:8765   (creative-kit studio)"},
        "more": "help(operation=<name>) for arguments and examples",
    }


@op(examples=[{}])
def campaigns(ctx: Context) -> list[dict]:
    """List the campaigns: markets, products, channels and creative types of each."""
    cfg = load_config(ctx.config_dir)
    return [{"id": cid, "name": c["name"], "brand": c.get("sub_brand") or c["brand"], "partner": c.get("partner"),
             "markets": c["markets"], "products": [p["id"] for p in c["products"]],
             "channels": c.get("channels", ["social"]), "creative_types": cfg.creative_types(c)}
            for cid, c in cfg.campaigns.items()]


@op(examples=[{"campaign": "spring-launch", "market": "jp"}, {"campaign": "summer-promo"}])
def explain(ctx: Context, campaign: str = "", market: str = "") -> dict | list[dict]:
    """Show which config file set each value for a market (the merge, layer by layer).
    With a campaign and a market: that market. Leave either out for every market."""
    cfg = load_config(ctx.config_dir)
    if campaign and market:
        return explain_market(cfg, cfg.campaigns[campaign], market)
    camps = [cfg.campaigns[campaign]] if campaign else list(cfg.campaigns.values())
    return [explain_market(cfg, c, m) for c in camps for m in c["markets"] if not market or m == market]


@op(examples=[{"campaign": "summer-promo"}])
def report(ctx: Context, campaign: str) -> dict:
    """The last run's report: totals, per-market results, cost and time."""
    return _report(ctx, campaign, "report.json")


@op(examples=[{"campaign": "spring-launch", "market": "jp", "creative": "influencer"}, {"campaign": "summer-promo", "status": "blocked_legal"}])
def deliverables(ctx: Context, campaign: str, market: str = "", status: str = "", creative: str = "") -> list[dict]:
    """Every deliverable of the last run, filtered by market, status (built/planned/blocked_legal) or creative type."""
    keep = lambda i: all(not want or i.get(k) == want for k, want in (("market", market), ("status", status), ("creative", creative)))
    return [i for i in _report(ctx, campaign, "plan.json") if keep(i)]


@op(examples=[{"path": "markets/jp.yaml", "section": "overrides.acme.legal"}])
def who(ctx: Context, path: str, section: str = "", action: str = "update") -> dict:
    """Which teams (and people) may read/create/update/delete a config file or one section of it."""
    access = Access.load(ctx.config_dir)
    return {"path": path, "section": section or None, "action": action, "protected": access.is_protected(section or None),
            "teams": access.who(action, path, section or None)}


@op(examples=[{"path": "markets/de.yaml", "section": "overrides.acme.marketing.cta", "user": "hans@example.com"}])
def can(ctx: Context, path: str, section: str = "", action: str = "update", user: str = "") -> dict:
    """Whether a person (default: whoever is acting) may do an action on a config file or section."""
    person = user or ctx.user
    if not person:
        raise ValueError("say who: user=... or act as someone")
    return {"user": person, "action": action, "path": path, "section": section or None,
            "allowed": Access.load(ctx.config_dir).can(person, action, path, section or None)}


@op(examples=[{"path": "markets/jp.yaml"}], cli="get")
def config_get(ctx: Context, path: str) -> dict:
    """Read one config file, e.g. markets/jp.yaml, with who owns it and its locked sections."""
    from .studio import describe_file
    return describe_file(Path(ctx.config_dir), Access.load(ctx.config_dir), path)


@op(examples=[{}])
def snapshot(ctx: Context) -> dict:
    """Everything the Studio shows, in one document: campaigns, markets, deliverables, config, teams, audit."""
    from .studio import snapshot as build
    return build(ctx.config_dir, ctx.out_root)


# ─────────────────────────────────────────────────────────────── write
@op(write=True, examples=[{"path": "markets/jp.yaml", "section": "overrides.acme.marketing.cta", "value": "今すぐチェック"}], cli="set")
def config_set(ctx: Context, path: str, section: str, value: Any) -> dict:
    """Change one value in a config file, e.g. path=markets/jp.yaml section=overrides.acme.marketing.cta.
    Checked against teams.yaml, audited, and undone if it would break the config."""
    f = (Path(ctx.config_dir) / path).resolve()
    if Path(ctx.config_dir).resolve() not in f.parents or not f.is_file():
        raise FileNotFoundError(f"no config file {path}")
    store, before = ConfigStore(ctx.config_dir, Access.load(ctx.config_dir)), f.read_text()
    changed = store.set(ctx.user, path, section, value)
    try:
        _validate(ctx)
    except Exception as e:
        f.write_text(before)
        store._log(ctx.user, "revert", path, [section])
        raise ValueError(f"not saved, the config would break: {type(e).__name__}: {e}") from e
    return {"path": path, "changed": changed, "user": ctx.user}


@op(write=True, examples=[{"campaign": "spring-launch", "provider": "mock", "markets": "jp"}])
def run(ctx: Context, campaign: str, provider: str = "", markets: str = "", plan_only: bool = False) -> dict:
    """Run a campaign: resolve, legal check, source images, render, brand-check, report.
    provider=mock costs nothing; cached images are reused at $0. markets is a comma list, e.g. jp,mx."""
    from .cli import run as engine_run
    r = engine_run(str(ctx.config_dir), campaign, str(ctx.out_root), provider or None,
                   markets.split(",") if markets else None, plan_only)
    return {"campaign": campaign, "provider": r["provider"], "totals": r["totals"], "efficiency": r["efficiency"],
            "markets": {m: {k: v for k, v in x.items() if k in ("built", "blocked", "legal_findings", "message")}
                        for m, x in r["markets"].items()},
            "output": str(Path(ctx.out_root) / "vanilla" / campaign)}


def context_from_env(config_dir: str = "config", out_root: str = "output", user: str | None = None) -> Context:
    return Context(Path(config_dir), Path(out_root), user or os.environ.get("PIPELINE_USER") or None)


def coerce(raw: str) -> Any:
    """A CLI value as YAML (so lists, numbers and booleans work), falling back to the plain string."""
    try:
        v = yaml.safe_load(raw)
        return raw if isinstance(v, dict) else v           # 'a: b' in a sentence is text, not a map
    except yaml.YAMLError:
        return raw
