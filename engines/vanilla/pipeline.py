"""The pipeline, in four phases:

  1. RESOLVE  per market: merged brand rules, localized message, legal check
  2. SOURCE   per (market, product): reuse an asset or generate one (in parallel)
  3. RENDER   per (market, product, channel, format, asset type): build or plan
  4. REPORT   plan.json, report.json, approvals, influencers

Asset types are a registry (GENERATORS). Only `static` has a generator today;
every other type a channel declares is written to the plan as `planned`, so the
client sees the full scope. Adding video = one function + one registry entry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image

from .checks import brand_check, legal_check
from .config import Config, MarketContext, resolve_market
from .render import render_static

log = logging.getLogger("pipeline")


@dataclass
class MarketPlan:
    ctx: MarketContext
    message: str
    message_source: str          # brief | translated | fallback_en
    cta: str | None
    legal_findings: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.legal_findings)


# ---------------------------------------------------------------- phase 1
def localize(campaign: dict, locale: str, translator) -> tuple[str, str]:
    if locale.startswith("en"):
        return campaign["message"], "brief"
    given = (campaign.get("localized_messages") or {}).get(locale)
    if given:
        return given, "brief"
    try:
        translated = translator.translate(campaign["message"], locale)
    except Exception as e:  # translation is a nice-to-have; never fail the run on it
        log.warning("translation to %s failed: %s", locale, e)
        translated = None
    if translated:
        return translated, "translated"
    return campaign["message"], "fallback_en"


def resolve_phase(cfg: Config, campaign: dict, markets: list[str], translator) -> list[MarketPlan]:
    plans = []
    for m in markets:
        ctx = resolve_market(cfg, campaign, m)
        message, source = localize(campaign, ctx.locale, translator)
        cta = ctx.brand.get("marketing", {}).get("cta")
        findings = legal_check({"message": message, "message_en": campaign["message"], "cta": cta or ""},
                               ctx.banned_words())
        if findings:
            log.warning("[%s] LEGAL BLOCK: %s", m, ", ".join(f["word"] for f in findings))
        plans.append(MarketPlan(ctx, message, source, cta, findings))
    return plans


# ---------------------------------------------------------------- phase 2
def build_prompt(product: dict, ctx: MarketContext, standards: dict, creative: str = "product") -> str:
    """The creative type's shot recipe, filled in for this product and market."""
    aud = ctx.market.get("audience", {})
    values = {
        "product.name": product["name"], "product.description": product.get("description", ""),
        "audience.age": aud.get("age", ""), "audience.traits": ", ".join(aud.get("traits", [])),
        "market.name": ctx.market["name"], "brand.tone": ctx.brand.get("marketing", {}).get("tone", ""),
        "brand.accent": ctx.brand["assets"]["colors"]["accent"],
    }
    shot = standards["types"][creative]["shot"]
    parts = [shot[k] for k in ("subject", "setting", "camera", "composition") if k in shot]
    text = " ".join(part.rstrip(".") + "." for part in parts)
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return f"{text} Mood: {values['brand.tone']}. {standards.get('global', {}).get('forbidden_in_image', '')}".strip()


def find_asset(root: Path, product_id: str, market_id: str) -> tuple[Path | None, str]:
    """Most specific wins: market asset -> global asset."""
    candidates = (
        (root / f"assets/markets/{market_id}/products/{product_id}.png", "reused_market"),
        (root / f"assets/products/{product_id}.png", "reused_global"),
    )
    for path, kind in candidates:
        if path.exists():
            return path, kind
    return None, "missing"


def source_one(root: Path, product: dict, ctx: MarketContext, provider, standards: dict,
               creative: str = "product") -> dict:
    """Reuse or generate the image for one (market, product, creative type)."""
    if standards["types"][creative].get("source", "reuse_or_generate") == "reuse_or_generate":
        path, kind = find_asset(root, product["id"], ctx.id)
        if path:
            return {"path": path, "kind": kind}
    prompt = build_prompt(product, ctx, standards, creative)
    key = hashlib.sha1(f"{provider.name}|{prompt}".encode()).hexdigest()[:10]
    cached = root / f"assets/generated/{product['id']}/{creative}/{ctx.id}-{key}.png"
    if cached.exists():  # generated on an earlier run -> reuse, no API cost
        return {"path": cached, "kind": "reused_generated", "prompt": prompt}
    t0 = time.time()
    img = provider.generate(prompt, product)
    cached.parent.mkdir(parents=True, exist_ok=True)
    img.save(cached)
    log.info("[%s] generated %s/%s in %.1fs (%s)", ctx.id, product["id"], creative, time.time() - t0, provider.name)
    return {"path": cached, "kind": "generated", "prompt": prompt, "seconds": round(time.time() - t0, 1)}


def source_key(standards: dict, creative: str) -> str:
    """Which creative type's image this one uses ('poster' borrows the product shot's)."""
    src = standards["types"][creative].get("source", "reuse_or_generate")
    return src if src in standards["types"] else creative


def provider_for(provider, cfg: Config, creative: str):
    """The type's preferred models, then the org default, then the org's fallbacks (deduplicated)."""
    genai = cfg.org.get("creative_resources", {}).get("genai", {})
    preferred = cfg.standards["types"][creative].get("models", [])
    fallbacks = genai.get("openrouter_fallback_models", [])
    return provider.for_models(preferred, fallbacks) if preferred or fallbacks else provider


def source_phase(cfg: Config, campaign: dict, plans: list[MarketPlan], provider, workers: int = 4) -> dict:
    """Keyed by (market, product, creative type whose image it is). Runs in parallel."""
    kinds = {source_key(cfg.standards, c) for c in cfg.creative_types(campaign)}
    jobs = {(p.ctx.id, prod["id"], c): (prod, p.ctx, c) for p in plans if not p.blocked
            for prod in campaign["products"] for c in kinds}
    sources = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {k: pool.submit(source_one, cfg.root, prod, ctx, provider_for(provider, cfg, c), cfg.standards, c)
                   for k, (prod, ctx, c) in jobs.items()}
        for k, fut in futures.items():
            try:
                sources[k] = fut.result()
            except Exception as e:  # one failed generation shouldn't sink the campaign
                log.error("[%s] could not source %s/%s: %s", *k, e)
                sources[k] = {"path": None, "kind": "error", "error": str(e)}
    return sources


# ---------------------------------------------------------------- phase 3
@dataclass(frozen=True)
class Deliverable:
    """One file the campaign needs: market × product × creative type × channel × format × asset type."""
    market: str
    product: str
    channel: str
    format: dict
    asset_type: str
    safe_zone: float = 0.06
    creative: str = "product"
    standard: dict = field(default_factory=dict, compare=False, hash=False)   # the type's treatment + rules
    source: str = "product"                                                    # whose image it uses

    @property
    def path(self) -> Path:
        return Path(self.market) / self.product / self.channel / self.creative / self.asset_type / f"{self.format['id']}.png"

    def record(self, **extra) -> dict:
        return {"market": self.market, "product": self.product, "creative": self.creative, "channel": self.channel,
                "format": self.format["id"], "asset_type": self.asset_type, "path": str(self.path), **extra}


def deliverables(cfg: Config, campaign: dict, market: str, product: dict) -> Iterator[Deliverable]:
    """Everything one product needs in one market: creative types × the channel registry."""
    for creative in cfg.creative_types(campaign):
        for ch_id in campaign.get("channels", ["social"]):
            channel = cfg.channels[ch_id]
            for fmt in channel["formats"]:
                for asset_type in channel["asset_types"]:
                    yield Deliverable(market, product["id"], ch_id, fmt, asset_type,
                                      channel.get("text", {}).get("safe_zone", 0.06), creative,
                                      cfg.standards["types"][creative], source_key(cfg.standards, creative))


@lru_cache(maxsize=32)
def _open(path: str) -> Image.Image:
    """Decode each source once, however many formats are cut from it (renderers never mutate it)."""
    return Image.open(path).convert("RGB")


def generate_static(d: Deliverable, image: Image.Image, plan: MarketPlan, root: Path):
    return render_static(image, d.format, plan.ctx.brand, plan.ctx.partner, plan.message, plan.cta,
                         plan.ctx.disclaimers(), root, d.safe_zone, d.standard)


# asset type -> generator. Anything a channel asks for that isn't here is "planned".
GENERATORS: dict[str, Callable] = {"static": generate_static}


def produce(d: Deliverable, plan: MarketPlan, source: dict, root: Path, out: Path) -> dict:
    """Build one deliverable, or say why not. Used by every engine."""
    if plan.blocked:
        return d.record(status="blocked_legal")
    if d.asset_type not in GENERATORS:
        return d.record(status="planned")
    if not source.get("path"):
        return d.record(status="error", error=source.get("error", "no source image"))

    image = _open(str(source["path"]))
    creative, layout, logo = GENERATORS[d.asset_type](d, image, plan, root)
    (out / d.path).parent.mkdir(parents=True, exist_ok=True)
    creative.save(out / d.path)
    checks = brand_check(creative, layout, logo, d.standard.get("rules", {}), plan.message)
    if not checks["pass"]:
        log.warning("[%s] brand check failed: %s", d.path, checks)
    return d.record(status="built", source=source["kind"], brand_checks=checks)


def render_phase(cfg: Config, campaign: dict, plans: list[MarketPlan], sources: dict, out: Path) -> list[dict]:
    return [produce(d, plan, sources.get((plan.ctx.id, prod["id"], d.source), {}), cfg.root, out)
            for plan in plans
            for prod in campaign["products"]
            for d in deliverables(cfg, campaign, plan.ctx.id, prod)]


# ---------------------------------------------------------------- phase 4
def write_market_files(plans: list[MarketPlan], out: Path) -> None:
    for p in plans:
        d = out / p.ctx.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "approvals.json").write_text(json.dumps({
            "market": p.ctx.id,
            "status": "blocked_legal" if p.blocked else "pending",
            "approvers": [{"role": a, "decision": None} for a in p.ctx.approvers],
        }, indent=2, ensure_ascii=False))
        aff = p.ctx.affiliate or {}
        (d / "influencers.json").write_text(json.dumps([
            {**i,
             "verified": False,  # from config, not yet checked against the live account
             "affiliate_code": (f"{p.ctx.id.upper()}-{i['handle'].lstrip('@').upper()[:10]}"
                                if aff.get("enabled") and i.get("role") == "affiliate" else None),
             "utm": f"utm_source={i['handle'].lstrip('@')}&utm_medium=influencer&utm_campaign={{campaign}}"}
            for i in p.ctx.influencers], indent=2, ensure_ascii=False))
