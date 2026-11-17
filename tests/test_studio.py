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


def post(url, body, user=None):
    headers = {"Content-Type": "application/json", **({"X-Acting-As": user} if user else {})}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_and_data_are_served_from_live_config(server):
    url, _ = server
    html = urllib.request.urlopen(url + "/").read().decode()
    assert "window.STUDIO_EDIT = true" in html and "__DATA__" not in html
    code, r = post(url + "/api/snapshot", {})
    assert code == 200 and set(r["result"]["config"]["markets"]) == {"us", "mx", "jp", "de"}


def test_regional_edit_is_saved_as_one_line_and_audited(server):
    url, tmp = server
    before = (tmp / "config/markets/jp.yaml").read_text()
    code, r = post(url + "/api/config_set", {"path": "markets/jp.yaml", "section": "overrides.acme.marketing.cta",
                                             "value": "今すぐチェック"}, user="yuki@example.com")
    assert code == 200 and r["result"]["changed"] == ["overrides.acme.marketing.cta"]
    after = (tmp / "config/markets/jp.yaml").read_text()
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 1 and "今すぐチェック" in changed[0][1]              # comments and layout kept
    assert len(before.splitlines()) == len(after.splitlines())
    audit = [json.loads(l) for l in (tmp / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["user"] == "yuki@example.com"


def test_locked_section_and_other_markets_are_refused(server):
    url, tmp = server
    before = (tmp / "config/markets/jp.yaml").read_text()
    code, r = post(url + "/api/config_set", {"path": "markets/jp.yaml", "section": "overrides.acme.legal.banned_words",
                                             "value": []}, user="yuki@example.com")
    assert code == 403 and "ask: legal" in r["error"]
    code, _ = post(url + "/api/config_set", {"path": "markets/jp.yaml", "section": "locale", "value": "en-US"}, user="nora@example.com")
    assert code == 403
    code, r = post(url + "/api/config_set", {"path": "markets/jp.yaml", "section": "locale", "value": "en-US"})
    assert code == 403 and "needs a person" in r["error"]                     # no one acting: no writes
    assert (tmp / "config/markets/jp.yaml").read_text() == before


def test_an_edit_that_breaks_the_config_is_rolled_back(server):
    url, tmp = server
    f = tmp / "config/campaigns/spring-launch.yaml"
    before = f.read_text()
    code, r = post(url + "/api/config_set", {"path": "campaigns/spring-launch.yaml", "section": "markets",
                                             "value": ["us", "fr"]}, user="carla@example.com")
    assert code == 400 and "not saved" in r["error"]
    assert f.read_text() == before
    assert json.loads((tmp / "audit.jsonl").read_text().splitlines()[-1])["action"] == "revert"


def test_paths_outside_config_and_output_are_refused(server):
    url, _ = server
    code, _ = post(url + "/api/config_set", {"path": "../README.md", "section": "x", "value": 1}, user="olivia@example.com")
    assert code == 404
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url + "/out/../../config/org.yaml")


# ─────────────────────────────────────────── one layer, four interfaces: every operation is in all of them
def test_every_operation_is_in_the_cli_the_api_and_mcp(server):
    import asyncio
    from engines.vanilla.cli import main
    from engines.vanilla.mcp_server import build
    from engines.vanilla.ops import OPS, Context
    url, tmp = server
    api = {o["name"] for o in json.loads(urllib.request.urlopen(url + "/api/ops").read())}
    tools = {t.name: t for t in asyncio.run(build(Context(tmp / "config", tmp / "output")).list_tools())}
    assert api == set(tools) == set(OPS)
    for name, o in OPS.items():
        assert tools[name].annotations.read_only_hint is (not o.write)
        with pytest.raises(SystemExit) as e:                                  # the CLI has a command for each
            main([o.cli, "--help"])
        assert e.value.code == 0


def test_the_same_question_gets_the_same_answer_everywhere(server, capsys):
    import asyncio
    from engines.vanilla.cli import main
    from engines.vanilla.mcp_server import build
    from engines.vanilla.ops import Context
    url, tmp = server
    args = {"path": "markets/jp.yaml", "section": "overrides.acme.legal"}
    _, r = post(url + "/api/who", args)
    main(["who", args["path"], args["section"], "--config", str(tmp / "config"), "--json"])
    cli = json.loads(capsys.readouterr().out)
    res = asyncio.run(build(Context(tmp / "config", tmp / "output")).call_tool("who", args))
    mcp = json.loads(res.content[0].text)
    assert r["result"] == cli == mcp and list(cli["teams"]) == ["legal"]


def test_every_help_example_runs(tmp_path, capsys):
    """help shows a CLI, API and MCP example for each operation; each CLI example must actually work."""
    import shlex
    from engines.vanilla.cli import main
    from engines.vanilla.ops import OPS, Context
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "assets", tmp_path / "assets", ignore=shutil.ignore_patterns("generated"))
    ctx = Context(tmp_path / "config", tmp_path / "output")
    OPS["run"](Context(ctx.config_dir, ctx.out_root, "carla@example.com"), campaign="spring-launch", provider="mock")
    OPS["run"](Context(ctx.config_dir, ctx.out_root, "carla@example.com"), campaign="summer-promo", provider="mock")
    capsys.readouterr()
    for name in OPS:
        h = OPS["help"](ctx, operation=name)
        assert h["examples"] and all({"cli", "api", "mcp"} <= set(e) for e in h["examples"])
        for ex in h["examples"]:
            argv = shlex.split(ex["cli"])[1:] + ["--config", str(ctx.config_dir), "--out", str(ctx.out_root), "--json"]
            assert ex["cli"].startswith("creative-kit ")
            argv = [("carla@example.com" if a == "you@example.com" else a) for a in argv]
            if name == "config_set":
                argv = [a.replace("carla@", "yuki@") for a in argv]
            assert main(argv) == 0, ex["cli"]
            json.loads(capsys.readouterr().out)                                # every operation prints JSON
    assert main([]) == 0 and "SERVE IT" in capsys.readouterr().out             # bare `creative-kit` = help
    assert main(["help", "set"]) == 0 and "creative-kit set" in capsys.readouterr().out


def test_cli_reads_like_a_sentence(tmp_path, capsys):
    """Natural arguments are bare, old flag forms still work, nobody-set locally means you (and you're refused)."""
    from engines.vanilla.cli import main
    shutil.copytree(ROOT / "config", tmp_path / "config")
    cfg = ["--config", str(tmp_path / "config")]
    assert main(["explain", "spring-launch", "jp", "--json"] + cfg) == 0
    bare = json.loads(capsys.readouterr().out)
    assert main(["explain", "--campaign", "spring-launch", "--market", "jp", "--json"] + cfg) == 0
    assert json.loads(capsys.readouterr().out) == bare and bare["market"] == "jp"
    assert main(["explain", "--json"] + cfg) == 0                             # no arguments: every market
    assert len(json.loads(capsys.readouterr().out)) == 6
    assert main(["set", "markets/jp.yaml", "locale", "en-US"] + cfg) == 3      # the local user isn't on a team
    assert "use --as" in capsys.readouterr().err
    assert main(["set", "markets/jp.yaml", "overrides.acme.marketing.cta", "今すぐチェック", "--as", "yuki@example.com"] + cfg) == 0
    assert "updated markets/jp.yaml" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="missing VALUE"):
        main(["set", "markets/jp.yaml", "locale"] + cfg)
