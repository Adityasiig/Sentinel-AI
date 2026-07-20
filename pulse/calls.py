"""Live calls — Phase 1 read-only, on-demand fleet-wide traffic view.

When an operator opens the Calls tab, Pulse fans out to every host over the
same agentless SSH mechanism the probes use and asks each box — in a single
read-only command — how much call traffic it is carrying right now. Nothing is
stored; this is a live snapshot, not a collector.

The "calls" number means something slightly different per stack, because each
vendor exposes a different honest signal, so every host also reports a `kind`:

  IVG  (FreeSWITCH) — `concurrent`  : live concurrent calls (`show calls count`)
  OPS  (OpenSIPS)   — `concurrent`  : active dialogs (MI `get_statistics`)
  VOSS (VOS3000)    — `recent5m`    : calls completed in the last 5 minutes.
        VOS writes a CDR only at call teardown, so a true concurrent count is
        not available from the database. The recent-CDR rate is the honest
        live-traffic signal, and is labelled distinctly so it is never summed
        into the concurrent total.

Everything issued to a box is strictly read-only: `fs_cli show`, an OpenSIPS MI
`get_statistics` query, and a single `SELECT COUNT(*)` against the VOS CDR table.
"""
from __future__ import annotations

import re

from . import ssh
from .inventory import Host, load_hosts

# ── per-role read-only commands ──────────────────────────────────────────
# FreeSWITCH: `show calls count` prints "<n> total." — pull the integer.
_CMD_IVG = "fs_cli -x 'show calls count' 2>/dev/null || true"

# OpenSIPS: the dialog module's active_dialogs statistic. The MI FIFO in /tmp is
# owned by the `opensips` user and kernel protected_fifos blocks even root from
# reading it, so when we land as root we drop to the opensips user; when we land
# as the service user directly we call the CLI as-is.
_CMD_OPS = (
    "if [ \"$(id -u)\" = 0 ]; then "
    "runuser -u opensips -- opensips-cli -x mi get_statistics active_dialogs 2>/dev/null; "
    "else opensips-cli -x mi get_statistics active_dialogs 2>/dev/null; fi || true"
)

# VOS3000: calls completed in the last 5 minutes (stoptime is epoch-ms).
_CMD_VOSS = (
    "command -v mysql >/dev/null && "
    "mysql -u root vos3000 -N -e "
    "\"select count(*) from e_cdr where stoptime >= (unix_timestamp()-300)*1000\" "
    "2>/dev/null || true"
)

_CMD = {"IVG": _CMD_IVG, "OPS": _CMD_OPS, "VOSS": _CMD_VOSS}
_KIND = {"IVG": "concurrent", "OPS": "concurrent", "VOSS": "recent5m"}

_RE_TOTAL = re.compile(r"(\d+)\s+total")
_RE_DIALOGS = re.compile(r"active_dialogs\"?\s*:\s*(\d+)")
_RE_INT = re.compile(r"^\s*(\d+)\s*$")


def _parse(role: str, out: str) -> int | None:
    out = (out or "").strip()
    if not out:
        return None
    if role == "IVG":
        m = _RE_TOTAL.search(out)
    elif role == "OPS":
        m = _RE_DIALOGS.search(out)
    else:  # VOSS
        m = _RE_INT.search(out)
    return int(m.group(1)) if m else None


async def _count(host: Host, sess) -> dict:
    cmd = _CMD.get(host.role)
    if not cmd:
        return {"calls": None, "kind": "unknown"}
    r = await sess.run(cmd, timeout=12)
    return {"calls": _parse(host.role, r.stdout), "kind": _KIND[host.role]}


async def snapshot() -> dict:
    """Fan out to the whole fleet and return a live calls snapshot.

    Shape: {hosts:[{hostname,ip,role,calls,kind,error}], roles:{...}, totals:{...}}.
    A host we can't reach or read reports calls=None with an error string rather
    than dropping out, so the operator sees "no contact" instead of a silent gap.
    """
    hosts = load_hosts()
    if not hosts:
        return {"hosts": [], "roles": {}, "totals": {"concurrent": 0, "voss_recent5m": 0}}

    results = await ssh.fan_out(hosts, _count)

    out_hosts: list[dict] = []
    roles: dict[str, dict] = {}
    concurrent_total = 0
    voss_recent_total = 0

    for host in hosts:
        res = results.get(host.hostname, {})
        val = (res.get("value") or {}) if res.get("ok") else {}
        calls = val.get("calls")
        kind = val.get("kind", _KIND.get(host.role, "unknown"))
        err = "" if res.get("ok") else (res.get("error") or "unreachable")

        out_hosts.append({
            "hostname": host.hostname, "ip": host.ip, "role": host.role,
            "calls": calls, "kind": kind, "error": err,
        })

        r = roles.setdefault(host.role, {"total": 0, "reporting": 0, "calls": 0, "kind": _KIND.get(host.role, "unknown")})
        r["total"] += 1
        if isinstance(calls, int):
            r["reporting"] += 1
            r["calls"] += calls
            if kind == "concurrent":
                concurrent_total += calls
            elif kind == "recent5m":
                voss_recent_total += calls

    # busiest first within the list is decided client-side; keep server order stable
    return {
        "hosts": out_hosts,
        "roles": roles,
        "totals": {"concurrent": concurrent_total, "voss_recent5m": voss_recent_total},
    }
