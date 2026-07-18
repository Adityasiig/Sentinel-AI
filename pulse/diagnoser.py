"""Diagnoser — the Advisor engine (Phase 2).

After every sweep it reads each host's latest probes and asks: does a *known*
failure pattern fire here? Known failures (matched by a vetted playbook) open an
`open` incident carrying the exact remediation. A `crit` probe that no playbook
even covers opens a `needs-human` incident — the honest "we don't have a vetted
fix for this" signal. Cleared conditions resolve their incidents.

It writes incidents/advisories only. It never touches a fleet box — remediation
stays a human action until Phase 3 puts it behind the governor.
"""
from __future__ import annotations

from . import db
from .playbooks import match_host, playbooks_for


def _latest_by_probe(host_id: str) -> dict[str, dict]:
    return {p["probe"]: p for p in db.latest_probes(host_id)}


def evaluate() -> dict:
    """Re-derive incidents from the current probe state. Returns a summary."""
    new_open = 0
    resolved = 0
    total_open = 0

    for host in db.all_hosts():
        hid = host["id"]
        role = host["role"]
        probes = _latest_by_probe(hid)
        if not probes:
            continue

        # probe names any playbook for this role is responsible for
        covered = {b.detect["probe"] for b in playbooks_for(role)}
        active_kinds: set[str] = set()

        # 1) known failures → open incident + snapshot the vetted fix
        for pb in match_host(role, probes):
            active_kinds.add(pb.id)
            iid, created = db.open_incident(hid, pb.id, status="open")
            if created:
                new_open += 1
                db.record_advisory(iid, pb.id, pb.command, pb.destructive)
                db.audit("diagnoser", "incident_open", target=hid,
                         detail={"kind": pb.id, "severity": pb.severity,
                                 "destructive": pb.destructive})

        # 2) crit probe with no playbook covering it → needs-human
        for name, p in probes.items():
            if p["status"] == "crit" and name not in covered:
                kind = f"unknown:{name}"
                active_kinds.add(kind)
                iid, created = db.open_incident(hid, kind, status="needs-human")
                if created:
                    new_open += 1
                    db.audit("diagnoser", "incident_open", target=hid,
                             detail={"kind": kind, "severity": "unknown",
                                     "value": p.get("value", "")})

        # 3) anything previously open that no longer fires → resolve
        cleared = db.resolve_stale_incidents(hid, active_kinds)
        resolved += len(cleared)
        for kind in cleared:
            db.audit("diagnoser", "incident_resolved", target=hid, detail={"kind": kind})

    total_open = len(db.open_incidents())
    return {"new": new_open, "resolved": resolved, "open": total_open}
