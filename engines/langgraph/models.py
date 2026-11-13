"""Pydantic schemas for the shared config contract.

The vanilla engine trusts the YAML; this engine validates it up front and
fails with a precise message ("campaign spring-launch: products needs >= 2")
before any API money is spent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HEX = r"^#[0-9A-Fa-f]{6}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")   # unknown keys pass through to the renderer


class Colors(_Model):
    # all optional: a sub-brand may override just one colour
    primary: str | None = Field(default=None, pattern=HEX)
    accent: str | None = Field(default=None, pattern=HEX)
    text: str | None = Field(default=None, pattern=HEX)


class BrandAssets(_Model):
    logo: str | None = None
    colors: Colors = Colors()
    font: str | None = None


class Brand(_Model):
    id: str
    name: str
    parent: str | None = None
    assets: BrandAssets = BrandAssets()
    marketing: dict = {}
    legal: dict = {}
    compliance: dict = {}


class Partner(_Model):
    id: str
    name: str
    active: bool = True
    assets: dict = {}
    legal: dict = {}
    approvers: list[str] = []


class Audience(_Model):
    age: str | None = None
    traits: list[str] = []


class Market(_Model):
    id: str
    name: str
    region: str
    locale: str = Field(pattern=r"^[a-z]{2}-[A-Z]{2}$")
    audience: Audience = Audience()
    overrides: dict[str, dict] = {}
    influencers: list[dict] = []
    affiliate_program: dict = {}


class Format(_Model):
    id: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Channel(_Model):
    id: str
    name: str
    formats: list[Format] = Field(min_length=1)
    asset_types: list[Literal["static", "animated", "video", "sound", "gif", "vector"]]


class Product(_Model):
    id: str
    name: str
    description: str = ""


class Campaign(_Model):
    id: str
    name: str
    brand: str
    sub_brand: str | None = None
    partner: str | None = None
    message: str = Field(min_length=1, max_length=120)
    localized_messages: dict[str, str] = {}
    products: list[Product] = Field(min_length=2)           # requirement: at least two products
    markets: list[str] = Field(min_length=1)
    channels: list[str] = ["social"]

    @field_validator("products")
    @classmethod
    def unique_products(cls, v):
        ids = [p.id for p in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate product ids: {ids}")
        return v


class Org(_Model):
    org: str
    approvers: dict = {}
    creative_resources: dict = {}
    velocity: dict = {}


class ConfigTree(BaseModel):
    org: Org
    brands: dict[str, Brand]
    partners: dict[str, Partner]
    markets: dict[str, Market]
    channels: dict[str, Channel]
    campaigns: dict[str, Campaign]

    @model_validator(mode="after")
    def cross_references(self):
        """Every id a campaign points at must exist."""
        for c in self.campaigns.values():
            for kind, ids, pool in (("brand", [c.brand, c.sub_brand], self.brands),
                                    ("partner", [c.partner], self.partners),
                                    ("market", c.markets, self.markets),
                                    ("channel", c.channels, self.channels)):
                for i in ids:
                    if i and i not in pool:
                        raise ValueError(f"campaign {c.id}: unknown {kind} '{i}'")
        for b in self.brands.values():
            if b.parent and b.parent not in self.brands:
                raise ValueError(f"brand {b.id}: unknown parent '{b.parent}'")
        return self


def _dir(path: Path) -> dict:
    out = {}
    for f in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        out[data.get("id", f.stem)] = data
    return out


def load_validated(config_dir: str | Path) -> ConfigTree:
    d = Path(config_dir)
    return ConfigTree(
        org=yaml.safe_load((d / "org.yaml").read_text()),
        brands=_dir(d / "brands"), partners=_dir(d / "partners"), markets=_dir(d / "markets"),
        channels=_dir(d / "channels"), campaigns=_dir(d / "campaigns"),
    )
