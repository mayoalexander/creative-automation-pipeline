"""Cost and budget controls: the spend ledger, estimates, enforcement, who owns the budget, the forecast."""
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from engines.vanilla.access import Access, Forbidden
from engines.vanilla.costs import OverBudget, forecast
from engines.vanilla.ops import OPS, Context

ROOT = Path(__file__).resolve().parent.parent
OLIVIA, FIONA, CARLA = "olivia@example.com", "fiona@example.com", "carla@example.com"


@pytest.fixture
def repo(tmp_path):
    """A copy of config and supplied assets, with an empty image cache, so counts are this test's own."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "assets", tmp_path / "assets", ignore=shutil.ignore_patterns("generated"))
    return tmp_path


def ctx(repo, user=None):
    return Context(repo / "config", repo / "output", user)


def ledger(repo):
    f = repo / "spend.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def test_a_mock_run_writes_one_ledger_line_per_image(repo):
    OPS["run"](ctx(repo, CARLA), campaign="spring-launch", provider="mock", markets="jp")
    lines = ledger(repo)
    assert len(lines) == 2 * 3                                    # 2 products x 3 image kinds (poster reuses product)
    assert {l["user"] for l in lines} == {CARLA} and {l["provider"] for l in lines} == {"mock"}
    assert {l["campaign"] for l in lines} == {"spring-launch"} and {l["market"] for l in lines} == {"jp"}
    assert sum(l["cost_usd"] for l in lines) == 0                 # mock is free
    assert sum(not l["cached"] for l in lines) == 5               # all but the one product with a jp photo of its own
    s = OPS["costs"](ctx(repo), month=lines[0]["at"][:7])
    assert s["totals"]["images"] == 6 and s["by_person"][0]["user"] == CARLA and s["recent_runs"][0]["images"] == 6


def test_estimate_counts_new_and_cached_with_the_runs_cache_key(repo):
    before = OPS["estimate"](ctx(repo), campaign="spring-launch", markets="jp,us", provider="mock")
    assert before["images"] == {"new": 10, "cached": 0, "supplied": 2} and before["cost_usd"] == 0
    OPS["run"](ctx(repo, CARLA), campaign="spring-launch", provider="mock", markets="jp")
    after = OPS["estimate"](ctx(repo), campaign="spring-launch", markets="jp,us", provider="mock")
    assert after["by_market"]["jp"] == {"new": 0, "cached": 5, "supplied": 1, "cost_usd": 0.0}
    assert after["by_market"]["us"]["new"] == 5
    paid = OPS["estimate"](ctx(repo), campaign="spring-launch", markets="jp", provider="openrouter")
    assert paid["images"]["new"] == 5 and paid["cost_usd"] == 0.2   # a different model is a different cache key
    assert len(list((repo / "assets/generated").rglob("*.png"))) == 5  # only the mock run's images exist


def test_a_run_over_a_cap_is_refused_before_anything_is_generated(repo, monkeypatch):
    from engines.vanilla import providers
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    monkeypatch.setattr(providers.OpenRouterImageProvider, "generate", lambda *a: pytest.fail("generated an image"))
    monkeypatch.setattr(providers, "_post_json", lambda *a, **k: pytest.fail("called a paid API"))
    OPS["config_set"](ctx(repo, OLIVIA), path="org.yaml", section="budget.per_campaign_usd.spring-launch", value=0.01)
    with pytest.raises(OverBudget, match=r"would take spring-launch to \$0\.20 of its \$0\.01 cap.*ask: ops, finance"):
        OPS["run"](ctx(repo, CARLA), campaign="spring-launch", provider="openrouter", markets="jp")
    with pytest.raises(Forbidden, match="may not run over budget"):            # only a budget owner may override
        OPS["run"](ctx(repo, CARLA), campaign="spring-launch", provider="openrouter", markets="jp", allow_over_budget=True)
    assert not (repo / "assets/generated").exists() and ledger(repo) == []
    assert OPS["estimate"](ctx(repo), campaign="spring-launch", markets="jp", provider="openrouter")["ask"] == ["ops", "finance"]


def test_only_ops_and_finance_change_the_budget(repo):
    a = Access.load(repo / "config")
    assert a.can(OLIVIA, "update", "org.yaml", "budget.monthly_usd") and a.can(FIONA, "update", "org.yaml", "budget.when_over")
    assert not a.can(FIONA, "update", "org.yaml", "velocity")               # finance owns the budget, nothing else
    with pytest.raises(Forbidden, match="ask: ops, finance"):
        OPS["config_set"](ctx(repo, CARLA), path="org.yaml", section="budget.monthly_usd", value=10_000)
    OPS["config_set"](ctx(repo, FIONA), path="org.yaml", section="budget.monthly_usd", value=750)
    assert "  monthly_usd: 750 " in (repo / "config/org.yaml").read_text()   # one line, comment kept


def test_forecast_projects_the_month_and_the_day_it_hits_the_budget():
    lines = [{"at": f"2026-02-{d:02d}T10:00:00", "cost_usd": 1.0} for d in range(1, 11)]   # $1 a day, 10 days
    lines.append({"at": "2026-01-31T10:00:00", "cost_usd": 99.0})                         # another month
    f = forecast(lines, "2026-02", 20, date(2026, 2, 10))
    assert (f["month_to_date_usd"], f["daily_rate_usd"], f["projected_usd"]) == (10, 1, 28)
    assert f["hits_budget_on"] == "2026-02-20" and f["status"] == "will exceed" and f["remaining_usd"] == 10
    assert forecast(lines, "2026-02", 5, date(2026, 2, 10))["hits_budget_on"] == "2026-02-05"   # already crossed
    assert forecast(lines, "2026-02", 50, date(2026, 2, 10))["hits_budget_on"] is None
    past = forecast(lines, "2026-02", 50, date(2026, 3, 15))                                  # a finished month
    assert past["projected_usd"] == 10 and past["days_elapsed"] == 28
