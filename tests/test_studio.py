"""The Studio's editor: every save goes through access.py, keeps the file's comments, and can't break the config."""
import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from engines.vanilla import studio

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    started = {}
    real = ThreadingHTTPServer

    def capture(addr, handler):
        srv = real(("127.0.0.1", 0), handler)
        started["srv"] = srv
        return srv
    monkeypatch.setattr(studio, "ThreadingHTTPServer", capture)
    threading.Thread(target=studio.serve, args=(tmp_path / "config", tmp_path / "output"), daemon=True).start()
    while "srv" not in started:
        pass
    yield f"http://127.0.0.1:{started['srv'].server_address[1]}", tmp_path
    started["srv"].shutdown()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_and_data_are_served_from_live_config(server):
    url, _ = server
    html = urllib.request.urlopen(url + "/").read().decode()
    assert "window.STUDIO_EDIT = true" in html and "__DATA__" not in html
    data = json.loads(urllib.request.urlopen(url + "/api/data").read())
    assert set(data["config"]["markets"]) == {"us", "mx", "jp", "de"}


def test_regional_edit_is_saved_as_one_line_and_audited(server):
    url, tmp = server
    before = (tmp / "config/markets/jp.yaml").read_text()
    code, r = post(url + "/api/set", {"user": "yuki@example.com", "path": "markets/jp.yaml",
                                      "section": "overrides.acme.marketing.cta", "value": "今すぐチェック"})
    assert code == 200 and r["changed"] == ["overrides.acme.marketing.cta"]
    after = (tmp / "config/markets/jp.yaml").read_text()
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 1 and "今すぐチェック" in changed[0][1]              # comments and layout kept
    assert len(before.splitlines()) == len(after.splitlines())
    audit = [json.loads(l) for l in (tmp / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["user"] == "yuki@example.com"


def test_locked_section_and_other_markets_are_refused(server):
    url, tmp = server
    before = (tmp / "config/markets/jp.yaml").read_text()
    code, r = post(url + "/api/set", {"user": "yuki@example.com", "path": "markets/jp.yaml",
                                      "section": "overrides.acme.legal.banned_words", "value": []})
    assert code == 403 and "ask: legal" in r["error"]
    code, _ = post(url + "/api/set", {"user": "nora@example.com", "path": "markets/jp.yaml", "section": "locale", "value": "en-US"})
    assert code == 403
    assert (tmp / "config/markets/jp.yaml").read_text() == before


def test_an_edit_that_breaks_the_config_is_rolled_back(server):
    url, tmp = server
    f = tmp / "config/campaigns/spring-launch.yaml"
    before = f.read_text()
    code, r = post(url + "/api/set", {"user": "carla@example.com", "path": "campaigns/spring-launch.yaml",
                                      "section": "markets", "value": ["us", "fr"]})
    assert code == 400 and "not saved" in r["error"]
    assert f.read_text() == before
    assert json.loads((tmp / "audit.jsonl").read_text().splitlines()[-1])["action"] == "revert"


def test_paths_outside_config_and_output_are_refused(server):
    url, _ = server
    code, _ = post(url + "/api/set", {"user": "olivia@example.com", "path": "../README.md", "section": "x", "value": 1})
    assert code == 404
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url + "/out/../../config/org.yaml")
