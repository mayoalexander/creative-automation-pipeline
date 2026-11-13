"""The same pipeline as a LangGraph state graph.

    START → validate → resolve ─┬─(Send × market×product)→ source_asset ─┐
                                └──────────(all blocked)──────────────────┤
                                                          plan_render ◄───┘
                                     ─(Send × market×product)→ render_creatives
                                                          → approval_gate → report → END

What the framework adds over the vanilla engine:
  - Pydantic validation before any API spend         (validate)
  - parallel fan-out / fan-in with Send               (source_asset, render_creatives)
  - per-node retry policy on GenAI calls              (source_asset)
  - human-in-the-loop approval per market             (approval_gate → interrupt)
  - checkpointing: a failed or paused run resumes without regenerating finished work

Pixel work (render.py, checks.py) and domain rules (config.py merge) are shared
with the vanilla engine on purpose: the framework orchestrates, it doesn't draw.
"""
from __future__ import annotations

import json
import operator
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send, interrupt
from PIL import Image

from engines.vanilla.checks import brand_check, legal_check
from engines.vanilla.config import load_config, resolve_market
from engines.vanilla.pipeline import MarketPlan, localize, source_one, write_market_files
from engines.vanilla.providers import build_providers
from engines.vanilla.render import render_static
from engines.vanilla.report import build_report, write_outputs

from .models import load_validated

ENGINE = "langgraph"
GENERATORS = {"static"}


class State(TypedDict, total=False):
    config_dir: str
    campaign_id: str
    out_root: str
    provider: str
    markets: list[str]
    auto_approve: bool
    started: float
    plans: list[dict]
    sources: Annotated[list[dict], operator.add]     # fan-in: each branch appends
    items: Annotated[list[dict], operator.add]
    approvals: Annotated[list[dict], operator.add]
    report: dict


@lru_cache(maxsize=4)
def _cfg(config_dir: str):
    return load_config(config_dir)


@lru_cache(maxsize=4)
def _providers(kind: str, config_dir: str):
    genai = _cfg(config_dir).org.get("creative_resources", {}).get("genai", {})
    return build_providers(kind, genai)


def _out(state: State) -> Path:
    return Path(state["out_root"]) / ENGINE / state["campaign_id"]


# ------------------------------------------------------------------ nodes
def validate(state: State) -> dict:
    tree = load_validated(state["config_dir"])          # raises pydantic.ValidationError
    if state["campaign_id"] not in tree.campaigns:
        raise ValueError(f"unknown campaign '{state['campaign_id']}'")
    return {"started": time.time()}


def resolve(state: State) -> dict:
    cfg = _cfg(state["config_dir"])
    campaign = cfg.campaigns[state["campaign_id"]]
    _, translator = _providers(state["provider"], state["config_dir"])
    plans = []
    for m in state.get("markets") or campaign["markets"]:
        ctx = resolve_market(cfg, campaign, m)
        message, source = localize(campaign, ctx.locale, translator)
        cta = ctx.brand.get("marketing", {}).get("cta")
        findings = legal_check({"message": message, "message_en": campaign["message"], "cta": cta or ""},
                               ctx.banned_words())
        plans.append({"market": m, "message": message, "message_source": source,
                      "cta": cta, "legal_findings": findings})
    return {"plans": plans}


def fan_out_sources(state: State):
    campaign = _cfg(state["config_dir"]).campaigns[state["campaign_id"]]
    sends = [Send("source_asset", {**state, "market": p["market"], "product": prod})
             for p in state["plans"] if not p["legal_findings"] for prod in campaign["products"]]
    return sends or "plan_render"


def source_asset(task: dict) -> dict:
    cfg = _cfg(task["config_dir"])
    ctx = resolve_market(cfg, cfg.campaigns[task["campaign_id"]], task["market"])
    image_provider, _ = _providers(task["provider"], task["config_dir"])
    src = source_one(cfg.root, task["product"], ctx, image_provider)
    return {"sources": [{**src, "path": str(src["path"]), "market": task["market"],
                         "product": task["product"]["id"]}]}


def plan_render(state: State) -> dict:
    return {}  # join point: runs once after every source_asset branch has finished


def fan_out_render(state: State):
    campaign = _cfg(state["config_dir"]).campaigns[state["campaign_id"]]
    return [Send("render_creatives", {**state, "plan": p, "product": prod})
            for p in state["plans"] for prod in campaign["products"]]


def render_creatives(task: dict) -> dict:
    cfg = _cfg(task["config_dir"])
    campaign = cfg.campaigns[task["campaign_id"]]
    plan, prod = task["plan"], task["product"]
    ctx = resolve_market(cfg, campaign, plan["market"])
    src = next((s for s in task.get("sources", [])
                if s["market"] == plan["market"] and s["product"] == prod["id"]), None)
    image = Image.open(src["path"]).convert("RGB") if src else None
    out, items = _out(task), []
    for ch_id in campaign.get("channels", ["social"]):
        channel = cfg.channels[ch_id]
        for fmt in channel["formats"]:
            for asset_type in channel["asset_types"]:
                rel = Path(plan["market"]) / prod["id"] / ch_id / asset_type / f"{fmt['id']}.png"
                item = {"market": plan["market"], "product": prod["id"], "channel": ch_id,
                        "format": fmt["id"], "asset_type": asset_type, "path": str(rel)}
                if plan["legal_findings"]:
                    item["status"] = "blocked_legal"
                elif asset_type not in GENERATORS:
                    item["status"] = "planned"
                elif image is None:
                    item.update(status="error", error="no source image")
                else:
                    creative, layout, logo = render_static(
                        image, fmt, ctx.brand, ctx.partner, plan["message"], plan["cta"],
                        ctx.disclaimers(), cfg.root, channel.get("text", {}).get("safe_zone", 0.06))
                    (out / rel).parent.mkdir(parents=True, exist_ok=True)
                    creative.save(out / rel)
                    item.update(status="built", source=src["kind"], brand_checks=brand_check(creative, layout, logo))
                items.append(item)
    return {"items": items}


def approval_gate(state: State) -> dict:
    """Pause for a human decision per market (unless auto_approve)."""
    cfg = _cfg(state["config_dir"])
    campaign = cfg.campaigns[state["campaign_id"]]
    decisions = []
    for p in state["plans"]:
        if p["legal_findings"]:
            decisions.append({"market": p["market"], "decision": "blocked_legal"})
            continue
        ctx = resolve_market(cfg, campaign, p["market"])
        built = [i["path"] for i in state["items"] if i["market"] == p["market"] and i["status"] == "built"]
        if state.get("auto_approve"):
            decision = "approved"
        else:
            decision = interrupt({"market": p["market"], "approvers": ctx.approvers,
                                  "message": p["message"], "creatives": built})
        decisions.append({"market": p["market"], "decision": decision, "approvers": ctx.approvers})
    return {"approvals": decisions}


def report(state: State) -> dict:
    cfg = _cfg(state["config_dir"])
    campaign = cfg.campaigns[state["campaign_id"]]
    plans = [MarketPlan(resolve_market(cfg, campaign, p["market"]), p["message"], p["message_source"],
                        p["cta"], p["legal_findings"]) for p in state["plans"]]
    sources = {(s["market"], s["product"]): s for s in state.get("sources", [])}
    out = _out(state)
    write_market_files(plans, out)
    for a in state.get("approvals", []):                 # record the human decisions
        f = out / a["market"] / "approvals.json"
        data = json.loads(f.read_text())
        data["status"] = a["decision"]
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    image_provider, _ = _providers(state["provider"], state["config_dir"])
    rep = build_report(ENGINE, cfg, campaign, plans, sources, state.get("items", []),
                       time.time() - state["started"], image_provider.name)
    rep["approvals"] = state.get("approvals", [])
    write_outputs(out, rep, state.get("items", []))
    return {"report": rep}


# ------------------------------------------------------------------ graph
def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("validate", validate)
    g.add_node("resolve", resolve)
    # Node timeouts need async nodes in LangGraph; the HTTP call itself has a timeout instead.
    g.add_node("source_asset", source_asset,
               retry_policy=RetryPolicy(max_attempts=3, initial_interval=2.0))
    g.add_node("plan_render", plan_render)
    g.add_node("render_creatives", render_creatives)
    g.add_node("approval_gate", approval_gate)
    g.add_node("report", report)

    g.add_edge(START, "validate")
    g.add_edge("validate", "resolve")
    g.add_conditional_edges("resolve", fan_out_sources, ["source_asset", "plan_render"])
    g.add_edge("source_asset", "plan_render")
    g.add_conditional_edges("plan_render", fan_out_render, ["render_creatives"])
    g.add_edge("render_creatives", "approval_gate")
    g.add_edge("approval_gate", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
