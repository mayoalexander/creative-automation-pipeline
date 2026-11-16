"""Stress test: how does the pipeline behave as an enterprise grows?

Builds a synthetic config (N markets x M products) from the real one, runs an engine
on it with the offline mock provider, and measures wall time, peak memory, files and
plan size. Real GenAI time is MODELLED from the measured ~6 s per image, because paying
for hundreds of images to find a breaking point would just be burning money.

  python scripts/stress.py --scales 4x2,10x4,25x6,50x10 [--engine vanilla|langgraph]
"""
from __future__ import annotations

import argparse
import copy
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GEN_SECONDS = 6.0          # measured: one OpenRouter image, gemini-2.5-flash-image
GEN_WORKERS = 4            # source_phase's thread pool
IMG_COST = 0.04


def build_config(dst: Path, n_markets: int, n_products: int) -> None:
    src = ROOT / "config"
    shutil.copytree(src, dst / "config")
    shutil.copytree(ROOT / "assets", dst / "assets", ignore=shutil.ignore_patterns("generated"))
    base = [yaml.safe_load((src / f"markets/{m}.yaml").read_text()) for m in ("us", "mx", "jp", "de")]
    for f in (dst / "config/markets").glob("*.yaml"):
        f.unlink()
    ids = []
    for i in range(n_markets):
        m = copy.deepcopy(base[i % 4])
        old = m["id"]
        m["id"] = f"{old}{i:03d}"
        m["name"] = f"{m['name']} {i}"
        if "overrides" in m and "acme" in m["overrides"] and "assets" in m["overrides"]["acme"]:
            m["overrides"]["acme"]["assets"]["logo"] = "assets/markets/jp/logo.png"
        (dst / f"config/markets/{m['id']}.yaml").write_text(yaml.safe_dump(m, allow_unicode=True))
        ids.append(m["id"])
    camp = yaml.safe_load((src / "campaigns/spring-launch.yaml").read_text())
    camp["id"] = camp["name"] = "stress"
    camp["markets"] = ids
    camp["products"] = [{"id": f"p{j:02d}", "name": f"Acme Product {j}", "description": "a bottle"} for j in range(n_products)]
    camp["channels"] = ["social"]
    for f in (dst / "config/campaigns").glob("*.yaml"):
        f.unlink()
    (dst / "config/campaigns/stress.yaml").write_text(yaml.safe_dump(camp, allow_unicode=True))


def run(engine: str, n_markets: int, n_products: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix="stress-"))
    try:
        build_config(work, n_markets, n_products)
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.time()
        if engine == "vanilla":
            from engines.vanilla.cli import run as go
            report = go(str(work / "config"), "stress", str(work / "out"), "mock")
        else:
            from engines.langgraph.cli import run as go
            report = go(str(work / "config"), "stress", str(work / "out"), "mock", auto_approve=True)
        secs = time.time() - t0
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out = work / "out" / engine / "stress"
        t = report["totals"]
        gens = n_markets * n_products  # the prompt includes the market's audience -> one image per market x product
        return {
            "engine": engine, "markets": n_markets, "products": n_products, "deliverables": t["built"] + sum(t["planned"].values()),
            "built": t["built"], "seconds": round(secs, 1), "per_creative_ms": round(1000 * secs / max(t["built"], 1)),
            "peak_rss_mb": round(max(rss, rss0) / (1024 * 1024 if sys.platform == "darwin" else 1024)),
            "plan_kb": round((out / "plan.json").stat().st_size / 1024),
            "output_mb": round(sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6, 1),
            "modelled_genai_min": round(gens * GEN_SECONDS / GEN_WORKERS / 60, 1),
            "modelled_genai_usd": round(gens * IMG_COST, 2),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="4x2,10x4,25x6")
    ap.add_argument("--engine", default="vanilla", choices=["vanilla", "langgraph"])
    a = ap.parse_args()
    import logging
    logging.disable(logging.WARNING)
    rows = []
    for s in a.scales.split(","):
        m, p = (int(x) for x in s.split("x"))
        r = run(a.engine, m, p)
        rows.append(r)
        print(json.dumps(r), flush=True)
