"""Load the YAML config tree and resolve it into one context per market.

Precedence (more specific wins):
    global brand -> sub-brand -> market overrides -> partner (if active)

Merge rules per section:
    override  - nested keys from the more specific layer replace the base
    add_only  - lists are unioned; a layer can ADD rules but never remove one

legal and compliance are add_only so a regional team can make the rules
stricter, never looser.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MERGE_RULES = {
    "assets": "override",
    "marketing": "override",
    "legal": "add_only",
    "compliance": "add_only",
}


# ---------------------------------------------------------------- loading
def _load_dir(path: Path) -> dict[str, dict]:
    """Load every *.yaml in a folder, keyed by its `id` field."""
    items = {}
    for f in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        items[data.get("id", f.stem)] = data
    return items


@dataclass
class Config:
    root: Path
    org: dict
    brands: dict[str, dict]
    partners: dict[str, dict]
    markets: dict[str, dict]
    channels: dict[str, dict]
    campaigns: dict[str, dict]


def load_config(config_dir: str | Path) -> Config:
    config_dir = Path(config_dir).resolve()
    return Config(
        root=config_dir.parent,
        org=yaml.safe_load((config_dir / "org.yaml").read_text()),
        brands=_load_dir(config_dir / "brands"),
        partners=_load_dir(config_dir / "partners"),
        markets=_load_dir(config_dir / "markets"),
        channels=_load_dir(config_dir / "channels"),
        campaigns=_load_dir(config_dir / "campaigns"),
    )


def load_env(path: str | Path = ".env") -> None:
    """Tiny .env reader (stdlib only). Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- merging
def _override(base: Any, layer: Any) -> Any:
    if isinstance(base, dict) and isinstance(layer, dict):
        out = dict(base)
        for k, v in layer.items():
            out[k] = _override(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(layer)


def _add_only(base: Any, layer: Any) -> Any:
    if isinstance(base, dict) and isinstance(layer, dict):
        out = dict(base)
        for k, v in layer.items():
            out[k] = _add_only(base[k], v) if k in base else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(layer, list):
        return base + [x for x in layer if x not in base]
    # A scalar in an add-only section can't loosen the base: keep the base value.
    return base if base is not None else copy.deepcopy(layer)


def merge_brand(base: dict, layer: dict) -> dict:
    """Merge one brand layer onto another, section by section."""
    out = copy.deepcopy(base)
    for section, value in (layer or {}).items():
        rule = MERGE_RULES.get(section, "override")
        if section not in out:
            out[section] = copy.deepcopy(value)
        elif rule == "add_only":
            out[section] = _add_only(out[section], value)
        else:
            out[section] = _override(out[section], value)
    return out


def brand_chain(cfg: Config, brand_id: str) -> list[dict]:
    """[root brand, ..., brand_id] following `parent` links."""
    chain, seen = [], set()
    while brand_id:
        if brand_id in seen:
            raise ValueError(f"brand parent cycle at {brand_id}")
        seen.add(brand_id)
        brand = cfg.brands[brand_id]
        chain.insert(0, brand)
        brand_id = brand.get("parent")
    return chain


# ---------------------------------------------------------------- context
@dataclass
class MarketContext:
    market: dict
    brand: dict                      # fully resolved brand rules for this market
    partner: dict | None             # resolved partner, or None if inactive here
    approvers: list[str]
    influencers: list[dict] = field(default_factory=list)
    affiliate: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.market["id"]

    @property
    def locale(self) -> str:
        return self.market["locale"]

    def banned_words(self) -> list[str]:
        words = list(self.brand.get("legal", {}).get("banned_words", []))
        if self.partner:
            words += [w for w in self.partner.get("legal", {}).get("banned_words", []) if w not in words]
        return words

    def disclaimers(self) -> list[str]:
        return list(self.brand.get("compliance", {}).get("disclaimers", []))


def resolve_market(cfg: Config, campaign: dict, market_id: str) -> MarketContext:
    if market_id not in cfg.markets:
        raise KeyError(f"campaign {campaign['id']} references unknown market '{market_id}'")
    market = cfg.markets[market_id]
    overrides = market.get("overrides", {})

    # 1) global brand -> sub-brand, then 2) this market's overrides, same order.
    # Market overrides come last, so a market beats a sub-brand: most specific wins.
    leaf = campaign.get("sub_brand") or campaign["brand"]
    brand_ids = [b["id"] for b in brand_chain(cfg, leaf)]
    brand: dict = {}
    for bid in brand_ids:
        brand = merge_brand(brand, cfg.brands[bid])
    for bid in brand_ids:
        brand = merge_brand(brand, overrides.get(bid, {}))
    brand["id"] = brand_ids[-1]

    partner = None
    if campaign.get("partner"):
        p = _override(cfg.partners[campaign["partner"]], overrides.get(campaign["partner"], {}))
        partner = p if p.get("active", True) else None

    org_approvers = cfg.org.get("approvers", {})
    approvers = list(org_approvers.get("global", []))
    approvers += org_approvers.get("by_market", {}).get(market_id, org_approvers.get("default", []))
    if partner:
        approvers += partner.get("approvers", [])

    return MarketContext(
        market=market,
        brand=brand,
        partner=partner,
        approvers=approvers,
        influencers=market.get("influencers", []),
        affiliate=market.get("affiliate_program", {}),
    )
