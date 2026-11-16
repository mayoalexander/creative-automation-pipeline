"""Run report: the metrics Adobe named (time saved, campaigns, efficiency)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


def build_report(engine: str, cfg, campaign: dict, plans, sources: dict, items: list[dict],
                 seconds: float, provider_name: str) -> dict:
    res = cfg.org.get("creative_resources", {})
    genai = res.get("genai", {})
    built = [i for i in items if i["status"] == "built"]
    generated = sum(1 for s in sources.values() if s.get("kind") == "generated")
    manual_min = res.get("manual_minutes_per_creative", 45) * len(built)

    by_market = {}
    for p in plans:
        mi = [i for i in items if i["market"] == p.ctx.id]
        src = Counter(s["kind"] for (m, *_), s in sources.items() if m == p.ctx.id)
        by_market[p.ctx.id] = {
            "locale": p.ctx.locale,
            "message": p.message,
            "message_source": p.message_source,
            "partner": p.ctx.partner["id"] if p.ctx.partner else None,
            "built": sum(1 for i in mi if i["status"] == "built"),
            "planned": sum(1 for i in mi if i["status"] == "planned"),
            "blocked": sum(1 for i in mi if i["status"] == "blocked_legal"),
            "errors": sum(1 for i in mi if i["status"] == "error"),
            "sources": dict(src),
            "legal_findings": p.legal_findings,
            "brand_checks_passed": f"{sum(1 for i in mi if i.get('brand_checks', {}).get('pass'))}/"
                                   f"{sum(1 for i in mi if i['status'] == 'built')}",
            "approvers": p.ctx.approvers,
        }

    planned_by_type = defaultdict(int)
    for i in items:
        if i["status"] == "planned":
            planned_by_type[i["asset_type"]] += 1

    target = cfg.org.get("velocity", {}).get("campaigns_per_month", {}).get("target")
    return {
        "engine": engine,
        "campaign": campaign["id"],
        "provider": provider_name,
        "totals": {
            "markets": len(plans),
            "products": len(campaign["products"]),
            "built": len(built),
            "planned": dict(planned_by_type),
            "blocked_legal": sum(1 for i in items if i["status"] == "blocked_legal"),
            "errors": sum(1 for i in items if i["status"] == "error"),
            "images_generated": generated,
            "images_reused": sum(1 for s in sources.values() if str(s.get("kind", "")).startswith("reused")),
            "brand_checks_passed": sum(1 for i in built if i["brand_checks"]["pass"]),
        },
        "efficiency": {
            "run_seconds": round(seconds, 1),
            "manual_estimate_hours": round(manual_min / 60, 1),
            "time_saved_hours": round(manual_min / 60 - seconds / 3600, 1),
            "genai_cost_usd": round(generated * genai.get("image_cost_usd", 0), 2),
            "velocity_target_campaigns_per_month": target,
            "compute_hours_for_target": round(seconds * target / 3600, 1) if target else None,
        },
        "markets": by_market,
    }


def print_report(r: dict) -> None:
    t, e = r["totals"], r["efficiency"]
    line = "─" * 74
    print(f"\n{line}\n {r['campaign'].upper()}  ·  engine: {r['engine']}  ·  provider: {r['provider']}\n{line}")
    print(f" {'market':<7}{'locale':<8}{'built':>6}{'planned':>9}{'blocked':>9}{'reuse/gen':>11}"
          f"{'brand ✓':>9}  message")
    for m, d in r["markets"].items():
        src = d["sources"]
        reused = sum(v for k, v in src.items() if k.startswith("reused"))
        generated = src.get("generated", 0)
        reuse_gen = f"{reused}/{generated}"
        msg = d["message"] + ("  (EN fallback)" if d["message_source"] == "fallback_en" else "")
        print(f" {m:<7}{d['locale']:<8}{d['built']:>6}{d['planned']:>9}{d['blocked']:>9}"
              f"{reuse_gen:>11}{d['brand_checks_passed']:>9}  {msg}")
    print(line)
    print(f" built {t['built']}  ·  planned {sum(t['planned'].values())} {t['planned'] or ''}  ·  "
          f"blocked {t['blocked_legal']}  ·  errors {t['errors']}")
    print(f" images reused {t['images_reused']}  ·  generated {t['images_generated']}  ·  "
          f"brand checks {t['brand_checks_passed']}/{t['built']}")
    print(f" run {e['run_seconds']}s  ·  manual ≈ {e['manual_estimate_hours']}h  ·  "
          f"saved ≈ {e['time_saved_hours']}h  ·  GenAI cost ${e['genai_cost_usd']}")
    print(f"{line}\n")


def write_outputs(out: Path, report: dict, items: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "plan.json").write_text(json.dumps(items, indent=2, ensure_ascii=False))
