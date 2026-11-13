"""Every engine must honour the same contract: same config in, same files out."""
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engines.langgraph.cli import run as run_langgraph
from engines.langgraph.models import load_validated
from engines.vanilla.cli import run as run_vanilla

CONFIG = str(Path(__file__).resolve().parent.parent / "config")
ENGINES = {"vanilla": run_vanilla, "langgraph": run_langgraph}


def built_paths(out: Path, engine: str, campaign: str) -> set[str]:
    plan = json.loads((out / engine / campaign / "plan.json").read_text())
    return {i["path"] for i in plan if i["status"] == "built"}


@pytest.mark.parametrize("campaign", ["spring-launch", "summer-promo"])
def test_engines_produce_identical_outputs(tmp_path, campaign):
    reports = {name: fn(CONFIG, campaign, str(tmp_path), "mock") for name, fn in ENGINES.items()}
    assert built_paths(tmp_path, "vanilla", campaign) == built_paths(tmp_path, "langgraph", campaign)
    v, g = reports["vanilla"]["totals"], reports["langgraph"]["totals"]
    for key in ("built", "planned", "blocked_legal", "errors", "brand_checks_passed"):
        assert v[key] == g[key], key
    for path in built_paths(tmp_path, "langgraph", campaign):
        assert (tmp_path / "langgraph" / campaign / path).exists()


def test_langgraph_human_approval_is_recorded(tmp_path):
    report = run_langgraph(CONFIG, "spring-launch", str(tmp_path), "mock", auto_approve=False,
                           decide=lambda p: "rejected" if p["market"] == "mx" else "approved")
    decisions = {a["market"]: a["decision"] for a in report["approvals"]}
    assert decisions == {"us": "approved", "mx": "rejected", "jp": "approved", "de": "approved"}
    saved = json.loads((tmp_path / "langgraph/spring-launch/mx/approvals.json").read_text())
    assert saved["status"] == "rejected"


def _broken_config(tmp_path: Path, edit) -> Path:
    import shutil
    dst = tmp_path / "config"
    shutil.copytree(CONFIG, dst)
    edit(dst)
    return dst


def test_validation_rejects_single_product_brief(tmp_path):
    def edit(d):
        f = d / "campaigns/spring-launch.yaml"
        data = yaml.safe_load(f.read_text())
        data["products"] = data["products"][:1]
        f.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(ValidationError, match="at least 2"):
        load_validated(_broken_config(tmp_path, edit))


def test_validation_rejects_unknown_market(tmp_path):
    def edit(d):
        f = d / "campaigns/spring-launch.yaml"
        data = yaml.safe_load(f.read_text())
        data["markets"].append("fr")
        f.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(ValidationError, match="unknown market 'fr'"):
        load_validated(_broken_config(tmp_path, edit))
