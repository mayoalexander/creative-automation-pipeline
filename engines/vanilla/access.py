"""Teams and access control for the config tree: one reusable module.

    access = Access.load("config")
    access.can("yuki@example.com", "update", "markets/jp.yaml", "overrides.acme.marketing.cta")  # True
    access.can("yuki@example.com", "update", "markets/jp.yaml", "overrides.acme.legal")          # False
    access.who("update", "markets/jp.yaml", "overrides.acme.legal")   # {'legal': ['lee@example.com']}

    store = ConfigStore("config", access)
    store.set("yuki@example.com", "markets/jp.yaml", "overrides.acme.marketing.cta", "今すぐチェック")

Rules (config/teams.yaml): grants add up and nothing is allowed unless granted; a PROTECTED
section is reachable only through a grant that names the section; an update is checked
section by section, so changing two sections needs permission for both.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

ACTIONS = ("read", "create", "update", "delete")


class Forbidden(PermissionError):
    pass


def _under(section: str, pattern: str) -> bool:
    """True if a dotted section sits at or below a dotted pattern ('*' matches one segment)."""
    s, p = section.split("."), pattern.split(".")
    return len(s) >= len(p) and all(fnmatchcase(a, b) for a, b in zip(s, p))


def changed_sections(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose values differ between two YAML trees."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new), key=str):
            path = f"{prefix}.{k}" if prefix else str(k)
            out += changed_sections(old[k], new[k], path) if k in old and k in new else [path]
        return out
    return [] if old == new else [prefix]


@dataclass(frozen=True)
class Grant:
    team: str
    actions: tuple[str, ...]
    file: str
    section: str | None


class Access:
    def __init__(self, spec: dict):
        self.teams = spec.get("teams", {})
        self.protected = spec.get("protected", [])
        if any("to" not in g for g in spec.get("grants", [])):
            raise ValueError("teams.yaml: every grant needs `to:` (a bare `on:` is read by YAML as true)")
        self.grants = [Grant(g["team"], tuple(g["actions"]), *(res.split("#", 1) + [None])[:2])
                       for g in spec.get("grants", []) for res in g["to"]]
        unknown = {g.team for g in self.grants} - set(self.teams) - {"*"}
        bad = {a for g in self.grants for a in g.actions} - set(ACTIONS)
        if unknown or bad:
            raise ValueError(f"teams.yaml: unknown teams {sorted(unknown)} or actions {sorted(bad)}")

    @classmethod
    def load(cls, config_dir: str | Path) -> "Access":
        return cls(yaml.safe_load((Path(config_dir) / "teams.yaml").read_text()) or {})

    def teams_of(self, user: str) -> set[str]:
        return {t for t, v in self.teams.items() if user in v.get("members", [])} | {"*"}

    def is_protected(self, section: str | None) -> bool:
        return section is not None and any(_under(section, p) for p in self.protected)

    def _grant_covers(self, g: Grant, action: str, path: str, section: str | None) -> bool:
        if action not in g.actions or not fnmatchcase(path, g.file):
            return False
        if g.section is None:                      # whole-file grant: everything but protected sections
            return not self.is_protected(section)
        return section is not None and _under(section, g.section)

    def can(self, user: str, action: str, path: str, section: str | None = None) -> bool:
        mine = self.teams_of(user)
        return any(g.team in mine and self._grant_covers(g, action, path, section) for g in self.grants)

    def who(self, action: str, path: str, section: str | None = None) -> dict[str, list[str]]:
        """Teams (and their members) allowed to do this: the hand-off and routing primitive."""
        return {g.team: self.teams[g.team].get("members", []) for g in self.grants
                if g.team != "*" and self._grant_covers(g, action, path, section)}


def round_trip_yaml():
    """The YAML writer for edits: keeps comments, quotes and layout (config files are written in its style)."""
    from ruamel.yaml import YAML
    rt = YAML()
    rt.preserve_quotes, rt.width = True, 4096
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


class ConfigStore:
    """Create / read / update / delete config files, every call checked and audited."""

    def __init__(self, config_dir: str | Path, access: Access, audit: str | Path | None = None):
        self.root = Path(config_dir)
        self.access = access
        self.audit = Path(audit) if audit else self.root.parent / "audit.jsonl"

    def _check(self, user: str, action: str, path: str, section: str | None = None) -> None:
        if not self.access.can(user, action, path, section):
            where = f"{path}#{section}" if section else path
            owners = self.access.who(action, path, section)
            raise Forbidden(f"{user} may not {action} {where}" + (f" (ask: {', '.join(owners)})" if owners else ""))

    def _log(self, user: str, action: str, path: str, sections: list[str] | None = None) -> None:
        with self.audit.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": user, "action": action,
                                "path": path, "sections": sections}, ensure_ascii=False) + "\n")

    def read(self, user: str, path: str) -> dict:
        self._check(user, "read", path)
        return yaml.safe_load((self.root / path).read_text()) or {}

    def create(self, user: str, path: str, data: dict) -> None:
        self._check(user, "create", path)
        if (self.root / path).exists():
            raise FileExistsError(path)
        (self.root / path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        self._log(user, "create", path)

    def update(self, user: str, path: str, data: dict) -> list[str]:
        old = yaml.safe_load((self.root / path).read_text()) or {}
        sections = changed_sections(old, data)
        for s in sections:                          # every changed section needs its own permission
            self._check(user, "update", path, s)
        if sections:
            (self.root / path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
            self._log(user, "update", path, sections)
        return sections

    def set(self, user: str, path: str, section: str, value: Any) -> list[str]:
        """Change one value, keeping the file's comments and layout, so the git diff is one line."""
        from ruamel.yaml.comments import CommentedSeq
        rt, f = round_trip_yaml(), self.root / path
        doc = rt.load(f.read_text()) or {}
        node = doc
        *parents, leaf = section.split(".")
        for k in parents:
            node = node.setdefault(k, {})
        old = node.get(leaf) if hasattr(node, "get") else None
        if isinstance(value, list) and getattr(getattr(old, "fa", None), "flow_style", lambda: False)():
            value = CommentedSeq(value)                 # a [a, b] list stays on one line
            value.fa.set_flow_style()
        node[leaf] = value
        sections = changed_sections(yaml.safe_load(f.read_text()) or {}, json.loads(json.dumps(doc)))
        for s in sections:
            self._check(user, "update", path, s)
        if sections:
            with f.open("w") as out:
                rt.dump(doc, out)
            self._log(user, "update", path, sections)
        return sections

    def delete(self, user: str, path: str) -> None:
        self._check(user, "delete", path)
        (self.root / path).unlink()
        self._log(user, "delete", path)
