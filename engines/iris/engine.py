"""The same workflow on IRIS: the pipeline core renders, IRIS runs the campaign.

    shared core (resolve -> source -> render)          same contract as the other engines
        │
        ▼
    IRIS as the system of record
      1. workspace   Atlas board per campaign, one list per stage
      2. dataset     every creative is a typed record, upserted by key (re-runs don't duplicate)
      3. approvals   one card per market, one checklist task per approver, creatives attached
      4. review      optional: an IRIS agent reviews each market's localized copy
      5. insights    dataset aggregates (built / blocked / brand-check pass rate per market)

    `sync-approvals` later reads the ticked checklists back into approvals.json and the dataset.

State (board, list, card ids) is kept in output/iris/<campaign>/iris_state.json so
re-running a campaign updates the same workspace instead of creating a new one.
"""
from __future__ import annotations

import json
from pathlib import Path

from engines.vanilla.cli import run as run_core
from engines.vanilla.config import load_config

from .client import IrisCLI

ENGINE = "iris"
DATASET = "creative-assets"
DATASET_FIELDS = [
    {"key": "market", "label": "Market", "type": "string", "required": True},
    {"key": "product", "label": "Product", "type": "string", "required": True},
    {"key": "channel", "label": "Channel", "type": "string", "required": True},
    {"key": "format", "label": "Format", "type": "string", "required": True},
    {"key": "asset_type", "label": "Asset type", "type": "string", "required": True},
    {"key": "status", "label": "Status", "type": "string", "required": True},
    {"key": "source", "label": "Source", "type": "string"},
    {"key": "brand_check_passed", "label": "Brand check passed", "type": "boolean"},
    {"key": "approval", "label": "Approval", "type": "string"},
    {"key": "campaign", "label": "Campaign", "type": "string", "required": True},
    {"key": "path", "label": "Path", "type": "string"},
]


def _state_file(out: Path) -> Path:
    return out / "iris_state.json"


def _load_state(out: Path) -> dict:
    f = _state_file(out)
    return json.loads(f.read_text()) if f.exists() else {}


def _save_state(out: Path, state: dict) -> None:
    _state_file(out).write_text(json.dumps(state, indent=2))


def ensure_workspace(iris: IrisCLI, campaign: dict, state: dict) -> dict:
    if state.get("bloq_id"):
        return state
    bloq = iris.run("atlas", "create", "--name", f"Creative Pipeline — {campaign['name']}",
                    "--description", f"Campaign {campaign['id']}: creatives, approvals and run records", "--json")
    state["bloq_id"] = iris.first_id(bloq, "bloq_id")
    for name in ("Approvals", "Runs"):
        lst = iris.run("atlas", "create-list", state["bloq_id"], name, "--json")
        state.setdefault("lists", {})[name] = iris.first_id(lst, "list_id")
    schema = iris.run("atlas:datasets", "schemas", "create", "--name", DATASET, "--slug", DATASET,
                      "--bloq", state["bloq_id"], "--fields", json.dumps(DATASET_FIELDS))
    state["dataset"] = DATASET if schema else None
    return state


def push_records(iris: IrisCLI, campaign_id: str, items: list[dict], approvals: dict, out: Path, state: dict) -> int:
    rows = [{
        "external_id": f"{campaign_id}/{i['path']}",
        "campaign": campaign_id,
        **{k: i[k] for k in ("market", "product", "channel", "format", "asset_type", "status", "path")},
        "source": i.get("source", ""),
        "brand_check_passed": bool(i.get("brand_checks", {}).get("pass")),
        "approval": approvals.get(i["market"], "pending"),
    } for i in items]
    f = out / "dataset_rows.json"
    f.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    iris.run("atlas:datasets", "import", str(f), "-s", DATASET, "--bloq", state["bloq_id"], "--json")
    return len(rows)


def approval_cards(iris: IrisCLI, campaign: dict, report: dict, items: list[dict], out: Path, state: dict) -> None:
    cards = state.setdefault("cards", {})
    for market, m in report["markets"].items():
        if market in cards or m["blocked"]:
            continue
        body = (f"Campaign: {campaign['name']}\nMarket: {market} ({m['locale']})\n"
                f"Message: {m['message']}  [{m['message_source']}]\nPartner: {m['partner'] or 'none'}\n"
                f"Built: {m['built']} · brand checks {m['brand_checks_passed']}\n\n"
                f"Tick your task to approve. Leave a comment to request changes.")
        card = iris.run("atlas", "add-item", state["bloq_id"], state["lists"]["Approvals"],
                        "--title", f"Approve {campaign['name']} — {market.upper()}", "--text", body, "--json")
        card_id = iris.first_id(card, "item_id")
        cards[market] = {"card_id": card_id, "tasks": {}}
        for approver in m["approvers"]:
            t = iris.run("atlas", "task-add", card_id, f"{approver}: approve {market.upper()} creatives", "--json")
            cards[market]["tasks"][approver] = iris.first_id(t, "task_id")
        previews = [out / i["path"] for i in items
                    if i["market"] == market and i["status"] == "built" and i["product"] == campaign["products"][0]["id"]]
        if previews:
            iris.run("atlas", "files", "upload", state["bloq_id"], *[str(p) for p in previews],
                     "--item", card_id, "--json")


def agent_review(iris: IrisCLI, agent_id: str, campaign: dict, report: dict) -> dict:
    """Ask an IRIS agent to review each market's localized headline for tone and cultural fit."""
    reviews = {}
    for market, m in report["markets"].items():
        prompt = (f"You review ad copy for brand {campaign['brand']}. Market {market} ({m['locale']}). "
                  f"Headline: \"{m['message']}\" (English source: \"{campaign['message']}\"). "
                  "Reply on one line: PASS or REVISE, then a reason under 20 words.")
        reviews[market] = iris.run("agents", "chat", agent_id, prompt, parse_json=False)
    return reviews


def run(config_dir: str, campaign_id: str, out_root: str, provider: str | None, publish: bool = False,
        markets: list[str] | None = None, agent_id: str | None = None) -> dict:
    report = run_core(config_dir, campaign_id, out_root, provider, markets, engine=ENGINE)
    items = report.pop("_items")
    out = Path(out_root) / ENGINE / campaign_id
    campaign = load_config(config_dir).campaigns[campaign_id]

    iris = IrisCLI(publish=publish)
    state = _load_state(out) if publish else {}
    state = ensure_workspace(iris, campaign, state)
    approvals = {m: ("blocked_legal" if d["blocked"] else "pending") for m, d in report["markets"].items()}
    n = push_records(iris, campaign_id, items, approvals, out, state)
    approval_cards(iris, campaign, report, items, out, state)
    if agent_id:
        report["agent_review"] = agent_review(iris, agent_id, campaign, report)
    if publish:
        _save_state(out, state)

    report["iris"] = {"mode": "publish" if publish else "dry-run", "records": n, "state": state,
                      "commands": iris.log}
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "iris_commands.sh").write_text("#!/bin/sh\n# commands this run " +
                                          ("executed" if publish else "would execute") + "\n" +
                                          "\n".join(iris.log) + "\n")
    return report


def sync_approvals(out_root: str, campaign_id: str) -> dict:
    """Read ticked approver tasks from IRIS back into approvals.json and the dataset."""
    out = Path(out_root) / ENGINE / campaign_id
    state = _load_state(out)
    if not state.get("cards"):
        raise SystemExit("nothing to sync: run with --publish first")
    iris = IrisCLI(publish=True)
    decisions = {}
    for market, card in state["cards"].items():
        tasks = iris.run("atlas", "tasks", card["card_id"], "--json")
        task_list = tasks.get("tasks", tasks) if isinstance(tasks, dict) else tasks
        done = [t for t in task_list if t.get("done") or t.get("completed") or t.get("status") == "done"]
        decisions[market] = "approved" if task_list and len(done) == len(task_list) else "pending"
        f = out / market / "approvals.json"
        data = json.loads(f.read_text())
        data["status"] = decisions[market]
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    plan = json.loads((out / "plan.json").read_text())
    push_records(iris, campaign_id, plan, decisions, out, state)
    return decisions
