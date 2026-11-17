"""Cost and budget controls: what generated images cost, who spent it, and whether a run fits.

    spend.jsonl (next to config/)   one line per image a run sourced: who, which campaign/market/model,
                                    cached or not, and what it cost (0 when cached or mock)
    org.yaml#budget                 monthly caps (org, per campaign, per market) and what to do when over;
                                    a locked section, so only Ops and Finance change it (teams.yaml)

    estimate(...)  ->  check(...)  ->  source images  ->  record(...)        (cli.run does all four)
    summarize(...) and forecast(...) read the ledger for `creative-kit costs` / `forecast` and the Studio.

    python -m engines.vanilla.costs backfill     seed the ledger from the generated-image cache, once
"""
from __future__ import annotations

import calendar
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .access import Access, Forbidden
from .pipeline import plan_source, source_jobs

log = logging.getLogger("costs")
LEDGER_ENV = "PIPELINE_SPEND_LEDGER"          # tests (and a hosted copy) can put the ledger elsewhere
BUDGET = ("org.yaml", "budget")               # where the caps live: the file and its locked section


class OverBudget(Forbidden):
    pass


# ───────────────────────────────────────────────────────────── the ledger
def ledger_path(config_dir: str | Path) -> Path:
    return Path(os.environ.get(LEDGER_ENV) or Path(config_dir).resolve().parent / "spend.jsonl")


def read_ledger(config_dir: str | Path) -> list[dict]:
    f = ledger_path(config_dir)
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def _append(config_dir: str | Path, lines: list[dict]) -> None:
    with ledger_path(config_dir).open("a") as f:
        f.writelines(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)


def image_cost(cfg, provider: str) -> float:
    """What one newly generated image costs. The offline mock is free."""
    return 0.0 if provider == "mock" else float(cfg.org.get("creative_resources", {}).get("genai", {}).get("image_cost_usd", 0))


def record(config_dir, cfg, campaign_id: str, user: str | None, provider: str, sources: dict, override: bool = False) -> list[dict]:
    """One ledger line per image a run sourced, from the source phase's results."""
    at, price = time.strftime("%Y-%m-%dT%H:%M:%S"), image_cost(cfg, provider)
    lines = [{"at": at, "run": f"{at} {campaign_id}", "user": user or "unknown", "campaign": campaign_id,
              "market": m, "product": p, "creative": c, "provider": provider, "model": s.get("model") or provider,
              "kind": s.get("kind"), "cached": s.get("kind") != "generated",
              "cost_usd": price if s.get("kind") == "generated" else 0.0, **({"override": True} if override else {})}
             for (m, p, c), s in sources.items()]
    _append(config_dir, lines)
    return lines


# ───────────────────────────────────────────────────────────── budget, estimate, check
def budget(cfg) -> dict:
    """org.yaml#budget, checked: caps are numbers (or absent), the per-* caps are maps of id -> number."""
    b = cfg.org.get("budget") or {}
    out = {"monthly_usd": b.get("monthly_usd"), "per_campaign_usd": b.get("per_campaign_usd") or {},
           "per_market_usd": b.get("per_market_usd") or {}, "when_over": b.get("when_over", "block")}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    if out["monthly_usd"] is not None and not num(out["monthly_usd"]):
        raise ValueError(f"budget.monthly_usd must be a number of dollars, not {out['monthly_usd']!r}")
    for k in ("per_campaign_usd", "per_market_usd"):
        if not isinstance(out[k], dict) or not all(num(v) for v in out[k].values()):
            raise ValueError(f"budget.{k} must be a map like {{spring-launch: 50}}, not {out[k]!r}")
        out[k] = dict(out[k])
    if out["when_over"] not in ("block", "warn"):
        raise ValueError(f"budget.when_over must be block or warn, not {out['when_over']!r}")
    return out


def _month(lines: list[dict], month: str) -> list[dict]:
    return [l for l in lines if l["at"].startswith(month)]


def _usd(x: float) -> float:
    return round(x + 1e-9, 2)


def estimate(cfg, campaign: dict, plans, provider, lines: list[dict], month: str | None = None) -> dict:
    """What a run would source (new vs reused, by the same cache key the run uses), what that costs,
    and whether it fits every cap this month. Generates nothing."""
    month = month or time.strftime("%Y-%m")
    price = image_cost(cfg, provider.name)
    by_market = defaultdict(lambda: {"new": 0, "cached": 0, "supplied": 0})
    for (m, *_), (product, ctx, creative, prov) in source_jobs(cfg, campaign, plans, provider).items():
        kind = plan_source(cfg.root, product, ctx, prov, cfg.standards, creative)["kind"]
        by_market[m][{"generate": "new", "reused_generated": "cached"}.get(kind, "supplied")] += 1
    for v in by_market.values():
        v["cost_usd"] = _usd(v["new"] * price)
    cost = _usd(sum(v["cost_usd"] for v in by_market.values()))
    spent = _month(lines, month)
    b, caps = budget(cfg), []

    def cap(scope, name, limit, already, adds, label):
        if limit is None:
            return
        after = _usd(already + adds)
        fits = adds <= 0 or after <= limit + 1e-9
        caps.append({"scope": scope, "name": name, "cap_usd": float(limit), "spent_usd": _usd(already), "adds_usd": _usd(adds),
                     "after_usd": after, "fits": fits,
                     **({} if fits else {"message": f"would take {label} to ${after:.2f} of its ${float(limit):.2f} cap"})})

    cap("org", cfg.org.get("org", "org"), b["monthly_usd"], sum(l["cost_usd"] for l in spent), cost, "the org's monthly spend")
    cid = campaign["id"]
    cap("campaign", cid, b["per_campaign_usd"].get(cid), sum(l["cost_usd"] for l in spent if l["campaign"] == cid), cost, cid)
    for m, v in by_market.items():
        cap("market", m, b["per_market_usd"].get(m), sum(l["cost_usd"] for l in spent if l["market"] == m), v["cost_usd"], f"market {m}")
    over = [c["message"] for c in caps if not c["fits"]]
    return {"campaign": cid, "provider": provider.name, "image_cost_usd": price, "month": month,
            "images": {k: sum(v[k] for v in by_market.values()) for k in ("new", "cached", "supplied")},
            "cost_usd": cost, "by_market": dict(by_market),
            "blocked_markets": [p.ctx.id for p in plans if p.blocked],
            "month_to_date_usd": _usd(sum(l["cost_usd"] for l in spent)), "budget_usd": b["monthly_usd"],
            "caps": caps, "fits": not over, "when_over": b["when_over"], **({"over": over} if over else {})}


def check(est: dict, access: Access, user: str | None, allow_over_budget: bool = False) -> None:
    """Refuse a run that would go over a cap (when_over: block), naming the cap and who can change it.
    allow_over_budget is honoured only for someone who may change the budget themselves."""
    if est["fits"]:
        if allow_over_budget:
            log.info("--allow-over-budget not needed: the run fits")
        return
    owners = ", ".join(access.who("update", *BUDGET)) or "ops"
    why = "; ".join(est["over"]) + f" (ask: {owners}, who own org.yaml#budget)"
    if allow_over_budget:
        if not (user and access.can(user, "update", *BUDGET)):
            raise Forbidden(f"{user} may not run over budget: {why}")
        log.warning("over budget, allowed by %s: %s", user, why)
    elif est["when_over"] == "warn":
        log.warning("over budget: %s", why)
    else:
        raise OverBudget(f"over budget: {why}")


# ───────────────────────────────────────────────────────────── reading the ledger
def _group(rows: list[dict], key: str) -> list[dict]:
    out = defaultdict(lambda: {"images": 0, "generated": 0, "cached": 0, "cost_usd": 0.0})
    for l in rows:
        g = out[l.get(key) or "unknown"]
        g["images"] += 1
        g["generated" if l["kind"] == "generated" else "cached"] += 1
        g["cost_usd"] += l["cost_usd"]
    return sorted(({key: k, **v, "cost_usd": _usd(v["cost_usd"])} for k, v in out.items()), key=lambda r: (-r["cost_usd"], str(r[key])))


def summarize(cfg, lines: list[dict], month: str = "", campaign: str = "") -> dict:
    """Spend for a month: totals, cache savings, by campaign / market / model / person / day, the caps, recent runs."""
    month = month or time.strftime("%Y-%m")
    rows = [l for l in _month(lines, month) if not campaign or l["campaign"] == campaign]
    savings = sum(image_cost(cfg, l["provider"]) for l in rows if l["kind"] == "reused_generated")
    for l in rows:
        l.setdefault("day", l["at"][:10])
    runs = defaultdict(list)
    for l in rows:
        runs[l["run"]].append(l)
    recent = [{"run": r, "at": ls[0]["at"], "campaign": ls[0]["campaign"], "user": ls[0]["user"], "provider": ls[0]["provider"],
               "markets": sorted({l["market"] for l in ls}), "images": len(ls), "generated": sum(l["kind"] == "generated" for l in ls),
               "cost_usd": _usd(sum(l["cost_usd"] for l in ls)), "source": ls[0].get("source", "run")}
              for r, ls in sorted(runs.items(), key=lambda kv: kv[1][0]["at"], reverse=True)[:12]]
    b, spent = budget(cfg), lambda pick: _usd(sum(l["cost_usd"] for l in _month(lines, month) if pick(l)))
    caps = ([{"scope": "org", "name": cfg.org.get("org", "org"), "cap_usd": b["monthly_usd"], "spent_usd": spent(lambda l: True)}]
            if b["monthly_usd"] is not None else [])
    caps += [{"scope": "campaign", "name": k, "cap_usd": v, "spent_usd": spent(lambda l, k=k: l["campaign"] == k)} for k, v in b["per_campaign_usd"].items()]
    caps += [{"scope": "market", "name": k, "cap_usd": v, "spent_usd": spent(lambda l, k=k: l["market"] == k)} for k, v in b["per_market_usd"].items()]
    return {"month": month, "campaign": campaign or None, "image_cost_usd": image_cost(cfg, "priced"), "when_over": b["when_over"],
            "totals": {"images": len(rows), "generated": sum(l["kind"] == "generated" for l in rows),
                       "cached": sum(l["kind"] != "generated" for l in rows), "cost_usd": _usd(sum(l["cost_usd"] for l in rows)),
                       "cache_savings_usd": _usd(savings)},
            "caps": caps,
            "by_campaign": _group(rows, "campaign"), "by_market": _group(rows, "market"), "by_model": _group(rows, "model"),
            "by_person": _group(rows, "user"), "by_day": sorted(_group(rows, "day"), key=lambda r: r["day"]),
            "recent_runs": recent}


def forecast(lines: list[dict], month: str, budget_usd: float | None, today: date) -> dict:
    """Month-to-date, daily run rate, projected month-end spend, and the date it would reach the budget."""
    y, m = map(int, month.split("-"))
    days = calendar.monthrange(y, m)[1]
    rows = _month(lines, month)
    mtd = sum(l["cost_usd"] for l in rows)
    elapsed = today.day if (today.year, today.month) == (y, m) else days if (today.year, today.month) > (y, m) else 0
    rate = mtd / elapsed if elapsed else 0.0
    projected = mtd + rate * (days - elapsed)
    hits = None
    if budget_usd is not None and mtd >= budget_usd > 0:                     # already there: the day it crossed
        total, per_day = 0.0, defaultdict(float)
        for l in rows:
            per_day[l["at"][:10]] += l["cost_usd"]
        for d in sorted(per_day):
            total += per_day[d]
            if total >= budget_usd:
                hits = d
                break
    elif budget_usd and rate > 0 and elapsed < days:
        day = elapsed + math.ceil((budget_usd - mtd) / rate)
        hits = (date(y, m, 1) + timedelta(days=day - 1)).isoformat() if day <= days else None
    return {"month": month, "days_in_month": days, "days_elapsed": elapsed, "month_to_date_usd": _usd(mtd),
            "daily_rate_usd": _usd(rate), "projected_usd": _usd(projected), "budget_usd": budget_usd,
            "remaining_usd": _usd(budget_usd - mtd) if budget_usd is not None else None,
            "projected_share": round(projected / budget_usd, 3) if budget_usd else None,
            "hits_budget_on": hits,
            "status": "no budget" if budget_usd is None else "over" if mtd > budget_usd
                      else "will exceed" if projected > budget_usd else "on track"}


# ───────────────────────────────────────────────────────────── backfill (once, from the image cache)
def backfill(config_dir: str | Path = "config") -> list[dict]:
    """Seed the ledger from assets/generated: one line per paid image, dated by the file's time.
    Each file is matched to campaign/market/model by recomputing the run's cache key; files from older
    prompts keep what the path says (product, creative, market) and an 'unknown' model. Mock images are skipped."""
    from PIL import Image
    from .config import load_config, resolve_market
    from .pipeline import build_prompt, provider_for, source_key
    from .providers import build_providers
    cfg = load_config(config_dir)
    if any(l.get("source") == "backfill" for l in read_ledger(config_dir)):
        raise ValueError("the ledger already has backfilled lines")
    genai = cfg.org.get("creative_resources", {}).get("genai", {})
    known = {}
    for kind in ("openrouter", "openai"):
        base = build_providers(kind, genai, api_key="-")[0]
        for cid, c in cfg.campaigns.items():
            for creative in {source_key(cfg.standards, t) for t in cfg.creative_types(c)}:
                prov = provider_for(base, cfg, creative)
                for mk in c["markets"]:
                    ctx = resolve_market(cfg, c, mk)
                    for p in c["products"]:
                        src = plan_source(cfg.root, p, ctx, prov, cfg.standards, creative)
                        if "prompt" in src:
                            known[src["path"].resolve()] = (cid, kind, prov.name)
    price, lines = image_cost(cfg, "priced"), []
    for f in sorted((cfg.root / "assets/generated").rglob("*.png")):
        with Image.open(f) as im:
            if im.convert("RGB").crop((0, 0, 48, 48)).getcolors(1):         # a flat corner: the offline mock
                continue
        rel = f.relative_to(cfg.root / "assets/generated").parts
        product, creative, market = rel[0], rel[1] if len(rel) == 3 else "product", f.stem.split("-")[0]
        cid, kind, model = known.get(f.resolve(), (None, "unknown", "unknown"))
        if not cid:
            owners = [k for k, c in cfg.campaigns.items() if market in c["markets"] and product in [p["id"] for p in c["products"]]]
            cid = owners[0] if len(owners) == 1 else "unknown"
        at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime))
        lines.append({"at": at, "run": f"backfill {at[:10]} {cid}", "user": "unknown (backfill)", "campaign": cid, "market": market,
                      "product": product, "creative": creative, "provider": kind, "model": model, "kind": "generated",
                      "cached": False, "cost_usd": price, "source": "backfill", "file": str(f.relative_to(cfg.root))})
    _append(config_dir, sorted(lines, key=lambda l: l["at"]))
    return lines


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] != ["backfill"]:
        raise SystemExit("usage: python -m engines.vanilla.costs backfill [CONFIG_DIR]")
    done = backfill(sys.argv[2] if len(sys.argv) > 2 else "config")
    print(f"backfilled {len(done)} images, ${sum(l['cost_usd'] for l in done):.2f}, into {ledger_path(sys.argv[2] if len(sys.argv) > 2 else 'config')}")
