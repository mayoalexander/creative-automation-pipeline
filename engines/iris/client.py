"""Thin wrapper over the `iris` CLI.

Two rules learned the hard way:
  1. Always pass an argv LIST to subprocess, never a shell string, so quotes,
     emoji and Japanese text inside arguments can't be re-split.
  2. Default to DRY mode: print the exact commands, change nothing. Publishing
     to a real workspace is an explicit --publish.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field


class IrisError(RuntimeError):
    pass


@dataclass
class IrisCLI:
    publish: bool = False
    binary: str = "iris"
    log: list[str] = field(default_factory=list)
    _fake_id: int = 900000

    def __post_init__(self):
        if self.publish and not shutil.which(self.binary):
            raise IrisError("`iris` CLI not found on PATH (install IRIS or run without --publish)")

    def run(self, *args: str, parse_json: bool = True):
        """Run `iris <args>`. In dry mode nothing runs: the command is logged and a fake id returned."""
        argv = [self.binary, *[str(a) for a in args]]
        self.log.append(shlex.join(argv))
        if not self.publish:
            self._fake_id += 1
            return {"id": self._fake_id, "item_id": self._fake_id, "success": True, "dry_run": True}
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise IrisError(f"{shlex.join(argv[:4])}… exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
        out = proc.stdout.strip()
        if not parse_json:
            return out
        match = re.search(r"[\[{].*[\]}]", out, re.S)   # some commands print a banner before the JSON
        if not match:
            raise IrisError(f"expected JSON from {shlex.join(argv[:4])}, got: {out[:300]}")
        return json.loads(match.group(0))

    @staticmethod
    def first_id(data, *keys: str) -> int:
        """IRIS commands name their id field differently (id, item_id, bloq.id...)."""
        if isinstance(data, dict):
            for k in keys + ("id", "item_id", "bloq_id", "list_id"):
                if isinstance(data.get(k), int):
                    return data[k]
            for v in data.values():
                if isinstance(v, dict):
                    try:
                        return IrisCLI.first_id(v, *keys)
                    except IrisError:
                        pass
        raise IrisError(f"no id in response: {str(data)[:200]}")
