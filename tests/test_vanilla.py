"""Tests for the rules worth defending: merge precedence, add-only legal,
word-boundary matching, and the output contract."""
import json
from pathlib import Path

import pytest

from engines.vanilla.checks import contrast_ratio, legal_check
from engines.vanilla.cli import run
from engines.vanilla.config import load_config, merge_brand, resolve_market

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


@pytest.fixture(scope="module")
def cfg():
    return load_config(CONFIG)


# ------------------------------------------------------------ merge rules
def test_add_only_section_cannot_remove_a_global_rule():
    base = {"legal": {"banned_words": ["cure", "miracle"]}}
    layer = {"legal": {"banned_words": ["milagro"]}}          # tries to "replace" the list
    merged = merge_brand(base, layer)
    assert merged["legal"]["banned_words"] == ["cure", "miracle", "milagro"]


def test_override_section_replaces_keys_but_keeps_siblings():
    base = {"assets": {"logo": "a.png", "colors": {"primary": "#000", "accent": "#111"}}}
    merged = merge_brand(base, {"assets": {"colors": {"primary": "#FFF"}}})
    assert merged["assets"]["colors"] == {"primary": "#FFF", "accent": "#111"}
    assert merged["assets"]["logo"] == "a.png"


def test_sub_brand_inherits_and_overrides(cfg):
    ctx = resolve_market(cfg, cfg.campaigns["spring-launch"], "us")
    assert ctx.brand["assets"]["colors"]["primary"] == "#0E7C66"   # from acme-fresh
    assert ctx.brand["assets"]["logo"] == "assets/brands/acme/logo.png"  # inherited from acme


def test_market_override_beats_brand_and_keeps_global_legal(cfg):
    ctx = resolve_market(cfg, cfg.campaigns["spring-launch"], "jp")
    assert ctx.brand["assets"]["logo"] == "assets/markets/jp/logo.png"
    assert "治る" in ctx.banned_words() and "guarantee" in ctx.banned_words()
    assert ctx.disclaimers() == ["※個人の感想です"]


def test_partner_switched_off_per_market(cfg):
    camp = cfg.campaigns["spring-launch"]
    assert resolve_market(cfg, camp, "us").partner["id"] == "sportco"
    assert resolve_market(cfg, camp, "jp").partner is None


def test_approvers_route_per_market(cfg):
    camp = cfg.campaigns["spring-launch"]
    # JP: global + JP-specific approvers; no partner approver because SportCo is off in JP
    assert resolve_market(cfg, camp, "jp").approvers == ["brand_lead", "jp_marketing", "jp_legal"]
    # US: global + default regional + partner approver (SportCo is active in the US)
    assert resolve_market(cfg, camp, "us").approvers == ["brand_lead", "regional_marketing",
                                                         "sportco_partnerships"]


# ------------------------------------------------------------ checks
def test_legal_matches_whole_words_only():
    assert legal_check({"m": "Secure your glow"}, ["cure"]) == []
    assert legal_check({"m": "A miracle CURE"}, ["cure"])[0]["word"] == "cure"


def test_legal_matches_cjk_substrings():
    assert legal_check({"m": "肌荒れが治る"}, ["治る"])[0]["word"] == "治る"


def test_brand_palette_is_legible():
    assert contrast_ratio((255, 255, 255), (0x0E, 0x7C, 0x66)) >= 4.5


# ------------------------------------------------------------ output contract
def test_output_contract(tmp_path):
    report = run(str(CONFIG), "spring-launch", str(tmp_path), "mock")
    out = tmp_path / "vanilla" / "spring-launch"
    assert report["totals"]["built"] == 4 * 2 * 3                      # markets x products x ratios
    assert report["totals"]["brand_checks_passed"] == report["totals"]["built"]
    for m in ("us", "mx", "jp", "de"):
        for p in ("shampoo", "lotion"):
            for r in ("1x1", "9x16", "16x9"):
                assert (out / m / p / "social" / "static" / f"{r}.png").exists()
        assert json.loads((out / m / "approvals.json").read_text())["status"] == "pending"
    plan = json.loads((out / "plan.json").read_text())
    assert {i["status"] for i in plan} == {"built", "planned"}


def test_market_specific_legal_block(tmp_path):
    report = run(str(CONFIG), "summer-promo", str(tmp_path), "mock")
    assert report["markets"]["mx"]["blocked"] > 0 and report["markets"]["mx"]["built"] == 0
    assert report["markets"]["us"]["built"] > 0


# ------------------------------------------------------------ explain (provenance)
def test_explain_names_the_layer_that_won(cfg):
    from engines.vanilla.explain import explain_market
    e = explain_market(cfg, cfg.campaigns["spring-launch"], "jp")
    by = {s["setting"]: e["layers"][s["layer"]]["file"] for s in e["settings"]}
    assert by["logo"] == "markets/jp.yaml"                 # market beats brand
    assert by["primary colour"] == "brands/acme-fresh.yaml"  # sub-brand beats parent
    tone = next(s["value"] for s in e["settings"] if s["setting"] == "tone")
    assert tone == "soft, seasonal (sakura)"                # caught a YAML flow-map comma bug once
    banned = e["lists"][0]["items"]
    assert {"value": "cure", "layer": 0} in banned and any(i["value"] == "治る" for i in banned)
    assert e["partner"]["active"] is False and e["partner"]["switched_off_by"] == "markets/jp.yaml"
