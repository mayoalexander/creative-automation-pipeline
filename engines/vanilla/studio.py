"""Creative Studio: the dashboard, served locally, with editing that goes through access control.

    creative-kit studio [--port 8765] [--env-file .env]

  GET  /                 the Studio, built from the LIVE config and the latest output
  GET  /out/<path>       rendered creatives, straight from output/vanilla
  GET  /api/ops          every operation (ops.py), with its arguments
  POST /api/<op>         call one, JSON body = its arguments; header X-Acting-As = who (write ops)

snapshot() is also what the hosted (read-only) page is built from, so both show the same thing.
Standard library only: the server is a thin shell around ConfigStore and the pipeline.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import yaml

from .access import Access, Forbidden
from .config import load_config
from .explain import explain_market
from .ops import OPS, Context, describe

UI = Path(__file__).resolve().parents[2] / "studio"


def _sections(data, prefix=""):
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out += [path] + _sections(v, path)
    return out


def describe_file(config_dir: Path, access: Access, rel: str) -> dict:
    """One config file: its data, who may edit or delete it, and its locked sections with their owners."""
    f = (Path(config_dir) / rel).resolve()
    if Path(config_dir).resolve() not in f.parents or not f.is_file():
        raise FileNotFoundError(f"no config file {rel}")
    data = yaml.safe_load(f.read_text()) or {}
    locked = [s for s in _sections(data) if access.is_protected(s)
              and not ("." in s and access.is_protected(s.rsplit(".", 1)[0]))]
    return {"file": rel, "data": data, "editors": access.who("update", rel), "deleters": access.who("delete", rel),
            "protected": [{"section": s, "editors": access.who("update", rel, s)} for s in locked]}


def snapshot(config_dir: str | Path, out_root: str | Path,
             image_url: Callable[[str, dict], str | None] | None = None) -> dict:
    """Everything the Studio shows, computed from config + the last run of each campaign."""
    config_dir, out_root = Path(config_dir), Path(out_root)
    cfg, access = load_config(config_dir), Access.load(config_dir)
    image_url = image_url or (lambda camp, item: f"/out/{camp}/{item['path']}")
    campaigns, markets, deliverables = [], [], []
    for cid, brief in cfg.campaigns.items():
        run = out_root / "vanilla" / cid
        report = json.loads((run / "report.json").read_text()) if (run / "report.json").exists() else None
        plan = json.loads((run / "plan.json").read_text()) if (run / "plan.json").exists() else []
        empty = {"built": 0, "planned": {}, "blocked_legal": 0, "errors": 0, "images_generated": 0,
                 "images_reused": 0, "brand_checks_passed": 0}
        campaigns.append({
            "id": cid, "name": brief["name"], "brand": brief["brand"], "sub_brand": brief.get("sub_brand"),
            "partner": brief.get("partner"), "message": brief["message"], "localized": brief.get("localized_messages") or {},
            "products": brief["products"], "markets": brief["markets"], "channels": brief.get("channels", ["social"]),
            "creative_types": cfg.creative_types(brief),
            "totals": report["totals"] if report else empty, "provider": report["provider"] if report else "not run",
            "efficiency": report["efficiency"] if report else {"run_seconds": 0, "manual_estimate_hours": 0, "genai_cost_usd": 0},
        })
        for m in brief["markets"]:
            rep = (report or {}).get("markets", {}).get(m, {})
            markets.append({**explain_market(cfg, brief, m), "message": rep.get("message", brief["message"]),
                            "message_source": rep.get("message_source", "brief"), "legal_findings": rep.get("legal_findings", []),
                            "counts": {k: rep.get(k, 0) for k in ("built", "planned", "blocked", "errors")}})
        for i in plan:
            deliverables.append({"campaign": cid, "market": i["market"], "product": i["product"], "creative": i.get("creative", "product"),
                                 "channel": i["channel"], "format": i["format"], "type": i["asset_type"], "status": i["status"],
                                 "source": i.get("source"), "path": i["path"], "checks": i.get("brand_checks"),
                                 "image": image_url(cid, i) if i["status"] == "built" else None})

    describe = lambda rel: describe_file(config_dir, access, rel)

    config = {}
    for kind in ("markets", "brands", "partners", "channels", "campaigns"):
        config[kind] = {}
        for f in sorted((config_dir / kind).glob("*.yaml")):
            d = describe(f"{kind}/{f.name}")
            ident = d["data"].get("id", f.stem)
            refs = lambda c: c["markets"] + [c["brand"], c.get("sub_brand"), c.get("partner")] + c["channels"]
            d["used_by"] = [c["id"] for c in campaigns if ident in refs(c)] if kind != "campaigns" else []
            config[kind][ident] = d
    config.update({"org": {"org": describe("org.yaml")}, "standards": {"creative_standards": describe("creative_standards.yaml")},
                   "teams": {"teams": describe("teams.yaml")}, "protected": access.protected,
                   "grants": [{"team": g.team, "actions": list(g.actions), "file": g.file, "section": g.section} for g in access.grants]})
    genai = cfg.org["creative_resources"]["genai"]
    audit = config_dir.parent / "audit.jsonl"
    return {
        "generated_from": "live config + output/vanilla (creative-kit studio)",
        "repo": "https://github.com/mayoalexander/creative-automation-pipeline",
        "org": {"approvers": cfg.org["approvers"], "velocity": cfg.org.get("velocity", {}),
                "genai": {k: genai.get(k) for k in ("image_provider", "openrouter_image_model", "openrouter_text_model")}},
        "campaigns": campaigns, "markets": markets, "deliverables": deliverables, "config": config,
        "standards": {k: {"name": v.get("name", k), "purpose": v.get("purpose", ""), "label": v.get("treatment", {}).get("label"),
                          "rules": v.get("rules", {}), "source": v.get("source"), "models": v.get("models", [])}
                      for k, v in cfg.standards.get("types", {}).items()},
        "teams": {k: {"name": v["name"], "members": v.get("members", [])} for k, v in access.teams.items()},
        "audit": [json.loads(l) for l in audit.read_text().splitlines()[-25:]] if audit.exists() else [],
    }


def page(data: dict, editable: bool) -> str:
    html = (UI / "app.html").read_text().replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    flag = "<script>window.STUDIO_EDIT = %s;</script>" % ("true" if editable else "false")
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Creative Studio</title><style>body{{margin:0}}{(UI / "app.css").read_text()}</style></head><body>{flag}{html}</body></html>')


def serve(config_dir: str = "config", out_root: str = "output", port: int = 8765) -> None:
    """The API is the operations registry over HTTP: GET /api/ops lists them, POST /api/<op> calls one.
    The Studio page is one client of it; curl, a script or another service are others."""
    config_dir, out_root = Path(config_dir).resolve(), Path(out_root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, page(snapshot(config_dir, out_root), True).encode(), "text/html; charset=utf-8")
            if path == "/api/ops":
                return self._json(200, describe())
            if path.startswith("/out/"):
                f = (out_root / "vanilla" / path[5:]).resolve()
                if out_root in f.parents and f.is_file():          # never serve outside output/
                    return self._send(200, f.read_bytes(), "image/png")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            name = self.path.split("?")[0].removeprefix("/api/")
            if not self.path.startswith("/api/") or name not in OPS:
                return self._json(404, {"ok": False, "error": f"no operation '{name}'", "ops": list(OPS)})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            ctx = Context(config_dir, out_root, self.headers.get("X-Acting-As") or None)
            t0 = time.time()
            try:
                result = OPS[name](ctx, **body)
            except Forbidden as e:
                return self._json(403, {"ok": False, "error": str(e)})
            except (FileNotFoundError, KeyError) as e:
                return self._json(404, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            except (TypeError, ValueError) as e:
                return self._json(400, {"ok": False, "error": str(e)})
            self._json(200, {"ok": True, "op": name, "seconds": round(time.time() - t0, 1), "result": result})

        def log_message(self, *args):
            pass

    print(f"Creative Studio + API on http://localhost:{port}  ({len(OPS)} operations, GET /api/ops)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
