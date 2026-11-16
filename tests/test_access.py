"""Teams and access control: who may read / create / update / delete which config."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from engines.vanilla.access import Access, ConfigStore, Forbidden, changed_sections
from engines.vanilla.config import load_config

CONFIG = Path(__file__).resolve().parent.parent / "config"
YUKI, LEE, PAT, CARLA = "yuki@example.com", "lee@example.com", "pat@example.com", "carla@example.com"


@pytest.fixture
def store(tmp_path):
    shutil.copytree(CONFIG, tmp_path / "config")
    return ConfigStore(tmp_path / "config", Access.load(tmp_path / "config"), audit=tmp_path / "audit.jsonl")


def test_everyone_reads_nobody_writes_by_default():
    a = Access.load(CONFIG)
    assert a.can("stranger@example.com", "read", "brands/acme.yaml")
    assert not a.can("stranger@example.com", "update", "brands/acme.yaml")


def test_regional_team_edits_its_market_but_not_its_legal_rules():
    a = Access.load(CONFIG)
    assert a.can(YUKI, "update", "markets/jp.yaml", "overrides.acme.marketing.cta")
    assert not a.can(YUKI, "update", "markets/jp.yaml", "overrides.acme.legal.banned_words")
    assert not a.can(YUKI, "update", "markets/mx.yaml")
    assert a.can(LEE, "update", "markets/jp.yaml", "overrides.acme.legal")


def test_who_routes_a_handoff_to_the_owning_team():
    assert Access.load(CONFIG).who("update", "markets/jp.yaml", "overrides.acme.legal") == {"legal": [LEE]}


def test_update_is_checked_section_by_section_and_audited(store, tmp_path):
    assert store.set(YUKI, "markets/jp.yaml", "overrides.acme.marketing.cta", "今すぐチェック") == ["overrides.acme.marketing.cta"]
    data = store.read(YUKI, "markets/jp.yaml")
    data["overrides"]["acme"]["marketing"]["cta"] = "again"
    data["overrides"]["acme"]["legal"]["banned_words"] = []          # sneaking a legal change into the same edit
    with pytest.raises(Forbidden, match="legal"):
        store.update(YUKI, "markets/jp.yaml", data)
    log = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert log == [{**log[0], "user": YUKI, "action": "update", "path": "markets/jp.yaml",
                    "sections": ["overrides.acme.marketing.cta"]}]    # the refused edit wrote nothing


def test_create_and_delete_follow_ownership(store):
    with pytest.raises(Forbidden):
        store.delete(YUKI, "partners/sportco.yaml")
    store.delete(PAT, "partners/sportco.yaml")
    store.create(CARLA, "campaigns/fall.yaml", {"id": "fall", "name": "Fall"})
    assert (store.root / "campaigns/fall.yaml").exists() and not (store.root / "partners/sportco.yaml").exists()


def test_changed_sections_names_the_leaves():
    assert changed_sections({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}, "d": 4}) == ["a.c", "d"]


def test_every_approver_is_a_real_team():
    teams = set(yaml.safe_load((CONFIG / "teams.yaml").read_text())["teams"])
    cfg = load_config(CONFIG)
    named = set(cfg.org["approvers"]["global"]) | set(cfg.org["approvers"]["default"])
    named |= {t for ts in cfg.org["approvers"]["by_market"].values() for t in ts}
    named |= {t for p in cfg.partners.values() for t in p.get("approvers", [])}
    assert named <= teams, named - teams
