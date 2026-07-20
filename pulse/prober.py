"""Prober — the read-only heartbeat of Phase 1.

Every `probe_interval` seconds it fans out to the fleet (concurrency-capped),
opens ONE SSH session per host, runs that host's role probes, evaluates each to
(status, value), and persists the batch. A single unreachable host becomes a
row of `unknown` probes rather than an exception that stalls the sweep.
"""
from __future__ import annotations

import asyncio
import time

from . import db, diagnoser, notifier
from .config import settings
from .inventory import Host, load_hosts
from .probes import probes_for
from .probes.base import UNKNOWN
from .ssh import HostSession, fan_out

_STATE = {"last_sweep": 0.0, "running": False, "sweep_ms": 0}


async def _probe_host(host: Host, sess: HostSession) -> list[dict]:
    """Run every probe for this host over the already-open session."""
    out: list[dict] = []
    for probe in probes_for(host.role):
        r = await sess.run(probe.command)
        status, value = probe.evaluate(r.stdout, r.ok)
        out.append({"probe": probe.name, "status": status, "value": value})
    return out


async def sweep_once() -> dict:
    """One full fleet sweep. Returns a small summary for logging/status."""
    hosts = load_hosts()
    if not hosts:
        return {"hosts": 0, "note": "empty inventory"}

    t0 = time.time()
    results = await fan_out(hosts, _probe_host)
    ts = time.time()

    reachable = 0
    for host in hosts:
        res = results.get(host.hostname, {})
        if res.get("ok") and res.get("value"):
            reachable += 1
            db.record_probes(host.hostname, res["value"], ts)
        else:
            # unreachable / no creds -> record all probes as unknown so the
            # dashboard shows "lost contact" instead of stale green.
            unknown = [{"probe": p.name, "status": UNKNOWN, "value": res.get("error", "unreachable")}
                       for p in probes_for(host.role)]
            db.record_probes(host.hostname, unknown, ts)

    # Phase 2: derive incidents/advisories from the fresh probe state.
    # Read-only w.r.t. the fleet — this only writes to our own DB.
    try:
        adv = diagnoser.evaluate()
    except Exception as e:  # noqa: BLE001 — advisor must never break the sweep
        adv = {"error": str(e)}
        print(f"[diagnoser] error: {e}", flush=True)

    # Phase 5: page on new incident open/resolve transitions. Blocking urllib
    # sends run off-loop so a slow webhook can't stall the sweep; dedup lives in
    # the DB so this is a no-op when nothing changed.
    try:
        alerts = await asyncio.to_thread(notifier.process)
        if alerts.get("opened") or alerts.get("resolved"):
            print(f"[notifier] {alerts}", flush=True)
    except Exception as e:  # noqa: BLE001 — alerting must never break the sweep
        print(f"[notifier] error: {e}", flush=True)

    ms = int((time.time() - t0) * 1000)
    _STATE.update(last_sweep=ts, sweep_ms=ms, incidents=adv.get("open", 0))
    db.audit("prober", "sweep",
             detail={"hosts": len(hosts), "reachable": reachable, "ms": ms, "advisor": adv})
    return {"hosts": len(hosts), "reachable": reachable, "ms": ms, "advisor": adv}


async def run_forever() -> None:
    """Background loop. Refreshes inventory into the DB, then sweeps on cadence."""
    _STATE["running"] = True
    # sync inventory -> hosts table once at boot (and it's cheap to redo each loop)
    while True:
        try:
            db.upsert_hosts([h.as_dict() for h in load_hosts()])
            summary = await sweep_once()
            print(f"[prober] sweep: {summary}", flush=True)
        except Exception as e:  # noqa: BLE001 — the loop must never die
            print(f"[prober] sweep error: {e}", flush=True)
        await asyncio.sleep(settings.probe_interval)


def state() -> dict:
    return dict(_STATE)
