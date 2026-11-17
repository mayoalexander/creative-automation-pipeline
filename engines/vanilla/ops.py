"""The pipeline's operations: the one layer every interface calls.

    UI (Studio) ──► API  /api/<op> ──┐
    CLI   python -m engines.vanilla op <op> ──┼──► OPS[<op>](ctx, **args) ──► config · access · engine
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

    def params(self) -> list[inspect.Parameter]:
        return list(inspect.signature(self.fn).parameters.values())[1:]      # without ctx

    def __call__(self, ctx: Context, **args) -> Any:
        if self.write and not ctx.user:
            raise Forbidden(f"'{self.name}' changes things, so it needs a person: --as, PIPELINE_USER or the Studio picker")
        return self.fn(ctx, **args)


OPS: dict[str, Op] = {}


def op(write: bool = False):
    def register(fn):
        OPS[fn.__name__] = Op(fn.__name__, fn, write, (fn.__doc__ or "").strip().splitlines()[0])
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
@op()
def campaigns(ctx: Context) -> list[dict]:
    """List the campaigns: markets, products, channels and creative types of each."""
    cfg = load_config(ctx.config_dir)
    return [{"id": cid, "name": c["name"], "brand": c.get("sub_brand") or c["brand"], "partner": c.get("partner"),
             "markets": c["markets"], "products": [p["id"] for p in c["products"]],
             "channels": c.get("channels", ["social"]), "creative_types": cfg.creative_types(c)}
            for cid, c in cfg.campaigns.items()]


@op()
def explain(ctx: Context, campaign: str, market: str) -> dict:
    """Show which config file set each value for one market of a campaign (the merge, layer by layer)."""
    cfg = load_config(ctx.config_dir)
    return explain_market(cfg, cfg.campaigns[campaign], market)


@op()
def report(ctx: Context, campaign: str) -> dict:
    """The last run's report: totals, per-market results, cost and time."""
    return _report(ctx, campaign, "report.json")


@op()
def deliverables(ctx: Context, campaign: str, market: str = "", status: str = "", creative: str = "") -> list[dict]:
    """Every deliverable of the last run, filtered by market, status (built/planned/blocked_legal) or creative type."""
    keep = lambda i: all(not want or i.get(k) == want for k, want in (("market", market), ("status", status), ("creative", creative)))
    return [i for i in _report(ctx, campaign, "plan.json") if keep(i)]


@op()
def who(ctx: Context, path: str, section: str = "", action: str = "update") -> dict:
    """Which teams (and people) may read/create/update/delete a config file or one section of it."""
    access = Access.load(ctx.config_dir)
    return {"path": path, "section": section or None, "action": action, "protected": access.is_protected(section or None),
            "teams": access.who(action, path, section or None)}


@op()
def can(ctx: Context, path: str, section: str = "", action: str = "update", user: str = "") -> dict:
    """Whether a person (default: whoever is acting) may do an action on a config file or section."""
    person = user or ctx.user
    if not person:
        raise ValueError("say who: user=... or act as someone")
    return {"user": person, "action": action, "path": path, "section": section or None,
            "allowed": Access.load(ctx.config_dir).can(person, action, path, section or None)}


@op()
def config_get(ctx: Context, path: str) -> dict:
    """Read one config file, e.g. markets/jp.yaml, with who owns it and its locked sections."""
    from .studio import describe_file
    return describe_file(Path(ctx.config_dir), Access.load(ctx.config_dir), path)


@op()
def snapshot(ctx: Context) -> dict:
    """Everything the Studio shows, in one document: campaigns, markets, deliverables, config, teams, audit."""
    from .studio import snapshot as build
    return build(ctx.config_dir, ctx.out_root)


# ─────────────────────────────────────────────────────────────── write
@op(write=True)
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


@op(write=True)
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
