"""Remediator — executes a vetted playbook against one host, safely.

Runs entirely inside a single SSH session so before/after state is captured on
the same connection that applies the fix:

    before(verify-probe)  ->  remediate steps  ->  after(verify-probe)
                                                     |
                                    pass ─► resolve incident, record success
                                    fail ─► run rollback, keep incident, record

Nothing here decides *whether* to run — that's the governor's job. The API calls
`governor.check(...)` first and only invokes this on an ALLOW. The session_runner
is injectable so the whole verify/rollback path is unit-testable with no fleet
contact.
"""
from __future__ import annotations

import json
import time

from . import db, ssh
from .config import settings
from .inventory import Host, load_hosts
from .probes import probes_for


def host_by_name(hostname: str) -> Host | None:
    for h in load_hosts():
        if h.hostname == hostname:
            return h
    return None


def _verify_probe(role: str, name: str):
    for p in probes_for(role):
        if p.name == name:
            return p
    return None


async def _run_probe(sess, role: str, name: str) -> dict:
    """Evaluate a single probe over an open session -> {status, value}."""
    probe = _verify_probe(role, name)
    if probe is None:
        return {"status": "unknown", "value": f"no probe '{name}'"}
    r = await sess.run(probe.command)
    status, value = probe.evaluate(r.stdout, r.ok)
    return {"status": status, "value": value}


async def execute(incident: dict, playbook, host: Host, approver: str,
                  *, session_runner=None) -> dict:
    """Apply `playbook` to `host`. Persists an action row + audit; returns result."""
    session_runner = session_runner or ssh.with_session
    role = host.role
    verify = playbook.verify or {}
    vprobe = verify.get("probe")
    expect = verify.get("expect", "ok")
    tmo = settings.remediation_timeout

    async def work(sess):
        before = await _run_probe(sess, role, vprobe) if vprobe else {}
        steps = []
        for step in playbook.remediate:
            r = await sess.run(step.get("cmd", ""), timeout=tmo)
            steps.append({"name": step.get("name", ""), "ok": r.ok,
                          "err": (r.error or "")[:400]})
        after = await _run_probe(sess, role, vprobe) if vprobe else {}
        ok = bool(vprobe) and after.get("status") == expect

        rolled = False
        if not ok and playbook.rollback:
            for step in playbook.rollback:
                await sess.run(step.get("cmd", ""), timeout=tmo)
            rolled = True
        return before, after, ok, rolled, steps

    started = time.time()
    try:
        before, after, ok, rolled, steps = await session_runner(host, work)
        err = ""
    except Exception as e:  # noqa: BLE001 — connection/exec failure is a failed remediation
        before, after, ok, rolled, steps = {}, {}, False, False, []
        err = str(e)

    result = "success" if ok else ("rolled_back" if rolled else "failed")
    aid = db.record_execution(
        incident_id=incident["id"], playbook_id=playbook.id, cmd=playbook.command,
        destructive=playbook.destructive, approver=approver, result=result,
        before=json.dumps(before), after=json.dumps(after),
    )
    if ok:
        db.set_incident_status(incident["id"], "resolved")

    db.audit(approver, "remediate", target=host.hostname, detail={
        "incident": incident["id"], "playbook": playbook.id, "result": result,
        "destructive": playbook.destructive, "before": before, "after": after,
        "error": err, "ms": int((time.time() - started) * 1000),
    })

    return {
        "action_id": aid, "result": result, "ok": ok, "rolled_back": rolled,
        "before": before, "after": after, "steps": steps, "error": err,
        "verify": {"probe": vprobe, "expect": expect},
    }
