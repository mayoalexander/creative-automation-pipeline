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

from engines.vanilla.checks import legal_check
from engines.vanilla.config import load_config, resolve_market
from engines.vanilla.pipeline import (MarketPlan, deliverables, localize, produce, provider_for, source_key,
                                     source_one, write_market_files)
from engines.vanilla.providers import build_providers
from engines.vanilla.report import build_report, write_outputs

from .models import load_validated

ENGINE = "langgraph"


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
    cfg = _cfg(state["config_dir"])
    campaign = cfg.campaigns[state["campaign_id"]]
    kinds = sorted({source_key(cfg.standards, c) for c in cfg.creative_types(campaign)})
    sends = [Send("source_asset", {**state, "market": p["market"], "product": prod, "creative": c})
             for p in state["plans"] if not p["legal_findings"] for prod in campaign["products"] for c in kinds]
    return sends or "plan_render"


def source_asset(task: dict) -> dict:
    cfg = _cfg(task["config_dir"])
    ctx = resolve_market(cfg, cfg.campaigns[task["campaign_id"]], task["market"])
    image_provider, _ = _providers(task["provider"], task["config_dir"])
    c = task["creative"]
    src = source_one(cfg.root, task["product"], ctx, provider_for(image_provider, cfg, c), cfg.standards, c)
    return {"sources": [{**src, "path": str(src["path"]), "market": task["market"],
                         "product": task["product"]["id"], "creative": c}]}


def plan_render(state: State) -> dict:
    return {}  # join point: runs once after every source_asset branch has finished


def fan_out_render(state: State):
    campaign = _cfg(state["config_dir"]).campaigns[state["campaign_id"]]
    return [Send("render_creatives", {**state, "plan": p, "product": prod})
            for p in state["plans"] for prod in campaign["products"]]


def render_creatives(task: dict) -> dict:
    """One (market, product) branch. The work itself is the shared produce()."""
    cfg = _cfg(task["config_dir"])
    campaign = cfg.campaigns[task["campaign_id"]]
    p, prod = task["plan"], task["product"]
    plan = MarketPlan(resolve_market(cfg, campaign, p["market"]), p["message"], p["message_source"],
                      p["cta"], p["legal_findings"])
    mine = {s["creative"]: s for s in task.get("sources", [])
            if s["market"] == p["market"] and s["product"] == prod["id"]}
    return {"items": [produce(d, plan, mine.get(d.source, {}), cfg.root, _out(task))
                      for d in deliverables(cfg, campaign, p["market"], prod)]}


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
    sources = {(s["market"], s["product"], s["creative"]): s for s in state.get("sources", [])}
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
