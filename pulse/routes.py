"""Route health — Phase 1 read-only, on-demand per-carrier view (VOS3000).

Routing and carrier/gateway definitions live only on the VOS3000 softswitches,
so this view is VOSS-only. When an operator picks a VOS box, Pulse opens one SSH
session and runs two read-only SELECTs against the local `vos3000` database:

  1. `e_gatewaymapping` — the configured carriers/gateways: name, lock state,
     capacity, routing priority and the peer IPs. Credential columns
     (`password`, `customerpassword`) are NEVER selected — only operational
     fields leave the box.
  2. `e_cdr` (last 15 min, grouped by termination gateway) — live call outcomes
     per carrier: attempts, answered, ASR (answer-seizure ratio) and ACD
     (average call duration), plus the fee booked in the window.

The two are joined by gateway name so each carrier row carries its config AND
its live outcome. A carrier with config but no recent CDRs simply shows "no
recent traffic" — which is itself a meaningful signal on this fleet, where CDRs
are archived aggressively and routing is frequently idle.

Nothing is written to the box. Read-only by contract.
"""
from __future__ import annotations

from . import ssh
from .inventory import Host, load_hosts

WINDOW_MIN = 15
_SPLIT = "___PULSE_SPLIT___"

# Config: operational columns only — no password/customerpassword ever.
_SQL_MAP = (
    "select name, locktype, capacity, priority, coalesce(remoteips,'') "
    "from e_gatewaymapping order by priority, name"
)

# Live outcomes over the recent window, grouped by termination (callee) gateway.
_SQL_CDR = (
    "select calleegatewayid, count(*), sum(holdtime>0), "
    "round(avg(case when holdtime>0 then holdtime end),1), "
    "round(coalesce(sum(fee),0),4) "
    f"from e_cdr where stoptime >= (unix_timestamp()-{WINDOW_MIN*60})*1000 "
    "group by calleegatewayid"
)

_CMD = (
    "command -v mysql >/dev/null || { echo __PULSE_NO_MYSQL__; exit 0; }; "
    f"mysql -u root vos3000 -BN -e \"{_SQL_MAP}\" 2>/dev/null; "
    f"echo {_SPLIT}; "
    f"mysql -u root vos3000 -BN -e \"{_SQL_CDR}\" 2>/dev/null"
)


class RouteError(RuntimeError):
    pass


def _to_int(s: str, default=0) -> int:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _to_float(s: str, default=0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def host_by_name(hostname: str) -> Host | None:
    for h in load_hosts():
        if h.hostname == hostname:
            return h
    return None


def _parse(out: str) -> list[dict]:
    """Turn the two-section mysql batch output into joined carrier rows."""
    if "__PULSE_NO_MYSQL__" in out:
        raise RouteError("VOS database unreachable (mysql client not found or mysqld down)")

    if _SPLIT in out:
        cfg_raw, cdr_raw = out.split(_SPLIT, 1)
    else:
        cfg_raw, cdr_raw = out, ""

    # live outcomes keyed by termination gateway id/name
    outcomes: dict[str, dict] = {}
    for line in cdr_raw.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        gw = parts[0].strip()
        attempts = _to_int(parts[1])
        answered = _to_int(parts[2])
        acd = _to_float(parts[3])
        fee = _to_float(parts[4])
        asr = round(answered / attempts * 100, 1) if attempts else 0.0
        outcomes[gw] = {"attempts": attempts, "answered": answered,
                        "asr": asr, "acd": acd, "fee": fee}

    carriers: list[dict] = []
    for line in cfg_raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            # pad short rows (mysql omits trailing empty cols rarely)
            parts += [""] * (5 - len(parts))
        name = parts[0].strip()
        locktype = _to_int(parts[1])
        capacity = _to_int(parts[2])
        priority = _to_int(parts[3])
        remote = parts[4].strip()
        live = outcomes.get(name, {})
        carriers.append({
            "name": name,
            "locktype": locktype,
            "status": "active" if locktype == 0 else "locked",
            "capacity": capacity,
            "priority": priority,
            "remote_ips": remote,
            "attempts": live.get("attempts", 0),
            "answered": live.get("answered", 0),
            "asr": live.get("asr"),          # None when no traffic
            "acd": live.get("acd"),
            "fee": live.get("fee", 0.0),
            "has_traffic": bool(live),
        })

    # outcomes with no matching config row (renamed/removed carrier still billing)
    unmatched = [gw for gw in outcomes if gw and gw not in {c["name"] for c in carriers}]
    for gw in unmatched:
        live = outcomes[gw]
        carriers.append({
            "name": gw, "locktype": None, "status": "unmapped",
            "capacity": 0, "priority": 9999, "remote_ips": "",
            "attempts": live["attempts"], "answered": live["answered"],
            "asr": live["asr"], "acd": live["acd"], "fee": live["fee"],
            "has_traffic": True,
        })
    return carriers


async def fetch(hostname: str) -> dict:
    """Pull carrier route health for one VOS3000 box. Read-only.

    Raises RouteError with a caller-mappable message on unknown host / wrong
    role / no creds / unreachable / db down.
    """
    host = host_by_name(hostname)
    if host is None:
        raise RouteError("unknown host")
    if host.role != "VOSS":
        raise RouteError("routes are VOS3000-only (pick a VOSS host)")
    if not host.has_credentials:
        raise RouteError("no SSH credentials for this host's role")

    async def _work(sess):
        return await sess.run(_CMD, timeout=20)

    try:
        result = await ssh.with_session(host, _work)
    except Exception as e:  # noqa: BLE001 — surface as a clean API error
        raise RouteError(f"unreachable: {e}") from e

    carriers = _parse(result.stdout or "")
    active = sum(1 for c in carriers if c["status"] == "active")
    with_traffic = sum(1 for c in carriers if c["has_traffic"])
    return {
        "hostname": hostname, "ip": host.ip, "role": host.role,
        "window_min": WINDOW_MIN,
        "carriers": carriers,
        "summary": {"total": len(carriers), "active": active, "with_traffic": with_traffic},
    }
