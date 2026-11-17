"""Explain a resolved market: which config file set each value.

    creative explain spring-launch jp
    creative explain --json > composition.json        # every market

It replays the same layers, in the same order, as resolve_market() and records the
last layer that wrote each setting. For add-only sections (legal, compliance) every
layer's contribution is kept, because nothing is ever replaced.
"""
from __future__ import annotations

from .config import MERGE_RULES, Config, brand_chain, resolve_market

TRACKED = [  # (label, path inside the brand dict)
    ("logo", ("assets", "logo")),
    ("primary colour", ("assets", "colors", "primary")),
    ("accent colour", ("assets", "colors", "accent")),
    ("tone", ("marketing", "tone")),
    ("call to action", ("marketing", "cta")),
]
ADD_ONLY = [("banned words", ("legal", "banned_words")), ("disclaimers", ("compliance", "disclaimers"))]


def _get(d: dict, path: tuple):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def explain_market(cfg: Config, campaign: dict, market_id: str) -> dict:
    market = cfg.markets[market_id]
    overrides = market.get("overrides", {})
    leaf = campaign.get("sub_brand") or campaign["brand"]
    chain = [b["id"] for b in brand_chain(cfg, leaf)]

    # the layers, in resolve_market()'s order
    layers = [{"file": f"brands/{bid}.yaml", "kind": "brand" if i == 0 else "sub-brand", "data": cfg.brands[bid]}
              for i, bid in enumerate(chain)]
    layers += [{"file": f"markets/{market_id}.yaml", "kind": "market", "for": bid, "data": overrides[bid]}
               for bid in chain if bid in overrides]

    settings = []
    for label, path in TRACKED:
        winner, value = None, None
        for i, layer in enumerate(layers):
            v = _get(layer["data"], path)
            if v is not None:
                winner, value = i, v
        settings.append({"setting": label, "value": value, "layer": winner})

    lists = []
    for label, path in ADD_ONLY:
        items = []
        for i, layer in enumerate(layers):
            for v in _get(layer["data"], path) or []:
                if v not in [x["value"] for x in items]:
                    items.append({"value": v, "layer": i})
        lists.append({"setting": label, "rule": MERGE_RULES[path[0]], "items": items})

    ctx = resolve_market(cfg, campaign, market_id)
    partner_id = campaign.get("partner")
    partner = None
    if partner_id:
        off = overrides.get(partner_id, {}).get("active") is False
        partner = {"id": partner_id, "file": f"partners/{partner_id}.yaml", "active": ctx.partner is not None,
                   "switched_off_by": f"markets/{market_id}.yaml" if off else None}
        if ctx.partner:
            for w in ctx.partner.get("legal", {}).get("banned_words", []):
                lists[0]["items"].append({"value": w, "layer": "partner"})

    # sanity: the explanation must agree with what the engine actually resolved
    for s, (_, path) in zip(settings, TRACKED):
        assert s["value"] == _get(ctx.brand, path), f"explain disagrees with resolve on {s['setting']}"

    return {
        "campaign": campaign["id"], "market": market_id, "market_name": market["name"],
        "locale": market["locale"], "audience": market.get("audience", {}),
        "layers": [{k: v for k, v in layer.items() if k != "data"} for layer in layers],
        "settings": settings, "lists": lists, "partner": partner, "approvers": ctx.approvers,
    }


def width(text: str) -> int:
    """Columns a string takes in a terminal: CJK characters take two."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, cols: int) -> str:
    text = str(text)
    while width(text) > cols - 1 and text:
        text = text[:-1]
    return text + " " * (cols - width(text))


def print_explain(e: dict) -> None:
    names = [layer["file"] + (f"  (overrides {layer['for']})" if layer.get("for") else "") for layer in e["layers"]]
    print(f"\n {e['campaign']} · {e['market_name']} ({e['locale']})\n")
    print(" layers, applied in order:")
    for i, n in enumerate(names):
        print(f"   {i + 1}. {n}")
    print("\n setting            value                              set by")
    for s in e["settings"]:
        src = names[s["layer"]] if s["layer"] is not None else "-"
        print(f"   {s['setting']:<17}{pad(s['value'], 35)}{src}")
    for lst in e["lists"]:
        print(f"\n {lst['setting']} ({lst['rule']}: layers add, never remove)")
        for it in lst["items"]:
            src = e["partner"]["file"] if it["layer"] == "partner" else names[it["layer"]]
            print(f"   + {pad(it['value'], 32)}{src}")
    if e["partner"]:
        p = e["partner"]
        state = "active" if p["active"] else f"OFF (switched off by {p['switched_off_by']})"
        print(f"\n partner {p['id']}: {state}")
    print(f" approvers: {', '.join(e['approvers'])}\n")
