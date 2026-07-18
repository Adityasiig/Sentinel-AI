"""Playbook registry — vetted, declarative remediations loaded from *.yaml.

A playbook maps a *detected* failure (a probe status, optionally guarded by an
`unless` rule so overlapping symptoms don't double-fire) to a human-readable
diagnosis and the exact remediation command(s) an operator should run.

Phase 2 (Advisor) only ever *reads* these: the diagnoser matches them against
live probe results and surfaces the diagnosis + command in the dashboard.
Pulse does not execute anything here — that's Phase 3, behind the governor.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Playbook:
    id: str
    role: str
    severity: str                      # critical | warning | info
    detect: dict                       # {probe, status:[...], value_contains?, unless?}
    diagnose: str
    remediate: list = field(default_factory=list)   # [{name, cmd, destructive}]
    verify: dict = field(default_factory=dict)       # {probe, expect}
    rollback: list = field(default_factory=list)

    # ── matching ─────────────────────────────────────────────────────────
    def matches(self, probes: dict[str, dict]) -> bool:
        """True if this playbook's detect rule fires for a host's latest probes.

        `probes` maps probe-name -> {"status": ..., "value": ...}.
        """
        d = self.detect
        p = probes.get(d["probe"])
        if not p:
            return False
        if p["status"] not in d.get("status", ["crit"]):
            return False
        vc = d.get("value_contains")
        if vc and vc not in (p.get("value") or ""):
            return False
        unless = d.get("unless")
        if unless:
            up = probes.get(unless["probe"])
            if up and up["status"] in unless.get("status", ["crit"]):
                return False  # a more specific playbook owns this symptom
        return True

    # ── presentation ─────────────────────────────────────────────────────
    @property
    def command(self) -> str:
        """The concatenated remediation an operator would run (advisory only)."""
        return "\n".join(
            (step.get("cmd") or "").strip() for step in self.remediate
        ).strip()

    @property
    def destructive(self) -> bool:
        return any(step.get("destructive") for step in self.remediate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "severity": self.severity,
            "diagnose": " ".join(self.diagnose.split()),
            "command": self.command,
            "destructive": self.destructive,
            "verify": self.verify,
            "steps": [
                {"name": s.get("name", ""), "destructive": bool(s.get("destructive"))}
                for s in self.remediate
            ],
        }


@lru_cache(maxsize=1)
def load_playbooks() -> list[Playbook]:
    """Load and cache every *.yaml playbook shipped next to this module."""
    books: list[Playbook] = []
    for path in sorted(glob.glob(os.path.join(_HERE, "*.yaml"))):
        with open(path) as f:
            for doc in yaml.safe_load_all(f):
                if not doc:
                    continue
                books.append(Playbook(
                    id=doc["id"],
                    role=doc["role"].upper(),
                    severity=doc.get("severity", "warning"),
                    detect=doc["detect"],
                    diagnose=doc.get("diagnose", ""),
                    remediate=doc.get("remediate", []) or [],
                    verify=doc.get("verify", {}) or {},
                    rollback=doc.get("rollback", []) or [],
                ))
    return books


def playbooks_for(role: str) -> list[Playbook]:
    role = role.upper()
    return [b for b in load_playbooks() if b.role == role]


def match_host(role: str, probes: dict[str, dict]) -> list[Playbook]:
    """All playbooks whose detect rule fires for this host's latest probes."""
    return [b for b in playbooks_for(role) if b.matches(probes)]
