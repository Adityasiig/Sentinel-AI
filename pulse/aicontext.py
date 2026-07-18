"""Build the non-secret context blocks the AI copilot is grounded on.

Everything here is derived from Pulse's own state store — hostnames, roles, IPs,
probe statuses/values, incident metadata. It deliberately never touches
credentials: SSH users/passwords live in `settings.creds`, and no function in
this module reads them. What the model sees is exactly what an operator sees on
the dashboard, nothing more.
"""
from __future__ import annotations

from . import db
from .probes import roll_up


def _host_line(h: dict) -> str:
    probes = db.latest_probes(h["id"])
    status = roll_up([p["status"] for p in probes])
    parts = [f"{p['probe']}={p['status']}" + (f"({p['value']})" if p.get("value") else "")
             for p in probes]
    return f"- {h['id']} [{h['role']}] {h['ip']} -> {status.upper()}: {', '.join(parts) or 'no data'}"


def fleet_context(max_hosts: int = 90) -> str:
    """Whole-fleet snapshot: role tallies + every non-OK host in detail.

    OK hosts are collapsed to a count so the prompt stays small and the model
    focuses on what's actually wrong.
    """
    hosts = db.all_hosts()
    if not hosts:
        return "No hosts in inventory yet."

    by_role: dict[str, dict[str, int]] = {}
    problem_lines: list[str] = []
    ok_count = 0
    for h in hosts:
        probes = db.latest_probes(h["id"])
        status = roll_up([p["status"] for p in probes])
        r = by_role.setdefault(h["role"], {"ok": 0, "warn": 0, "crit": 0, "unknown": 0})
        r[status] = r.get(status, 0) + 1
        if status == "ok":
            ok_count += 1
        else:
            problem_lines.append(_host_line(h))

    tally = "; ".join(
        f"{role}: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        for role, counts in sorted(by_role.items())
    )
    out = [f"Fleet: {len(hosts)} hosts. {tally}.", f"Healthy (OK): {ok_count}."]
    if problem_lines:
        out.append("Hosts needing attention:")
        out.extend(problem_lines[:max_hosts])
    else:
        out.append("No hosts are currently degraded.")
    return "\n".join(out)


def incident_context(inc: dict) -> str:
    """Context block for one incident: host, role, failing probes + recent history."""
    host_id = inc["host_id"]
    lines = [
        f"Host: {host_id} ({inc.get('ip', '?')}), role {inc.get('role', '?')}.",
        f"Incident kind: {inc['kind']}, status: {inc['status']}.",
    ]
    probes = db.latest_probes(host_id)
    if probes:
        lines.append("Current probes:")
        for p in probes:
            v = f" ({p['value']})" if p.get("value") else ""
            lines.append(f"  - {p['probe']}: {p['status']}{v}")
    # short history on the failing probes so the model can see a trend
    crit = [p["probe"] for p in probes if p["status"] in ("crit", "warn")]
    for name in crit[:4]:
        hist = db.probe_history(host_id, name, limit=8)
        seq = ", ".join(f"{h['status']}" for h in reversed(hist))
        if seq:
            lines.append(f"History[{name}] (old->new): {seq}")
    return "\n".join(lines)
