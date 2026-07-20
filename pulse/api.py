"""FastAPI surface — Phase 1 (read-only).

Serves the dashboard SPA and a small JSON API the SPA polls. Every /api route is
token-gated when PULSE_TOKEN is set. The prober background loop is started on
app startup so the fleet stays fresh even with no browser open (the SPA reads
cached state — Grafana-style stale-while-revalidate).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import __build__, __version__, aicontext, db, governor, llm, prober, remediator
from .config import settings
from .inventory import load_hosts
from .probes import probes_for, roll_up
from .playbooks import load_playbooks

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(HERE), "web")

app = FastAPI(title="Pulse", version=__version__, docs_url=None, redoc_url=None)


# ── auth ─────────────────────────────────────────────────────────────────
async def require_token(x_pulse_token: str = Header(default="")):
    if settings.token and x_pulse_token != settings.token:
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return True


# ── lifecycle ────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    db.init_db()
    db.upsert_hosts([h.as_dict() for h in load_hosts()])
    asyncio.create_task(prober.run_forever())


# ── public ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__, "build": __build__}


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status():
    st = prober.state()
    return {
        "version": __version__,
        "auth_required": bool(settings.token),
        "remediation_enabled": settings.remediation_enabled,
        "probe_interval": settings.probe_interval,
        "last_sweep": st.get("last_sweep", 0),
        "sweep_ms": st.get("sweep_ms", 0),
        "incidents": st.get("incidents", 0),
        "governor": governor.status(),
        "ai": {"available": llm.available(), "model": settings.llm_model if llm.available() else None},
    }


def _host_view(host: dict) -> dict:
    probes = db.latest_probes(host["id"])
    status = roll_up([p["status"] for p in probes])
    last_ts = max((p["ts"] for p in probes), default=0)
    return {
        "hostname": host["id"], "ip": host["ip"], "role": host["role"],
        "status": status, "last_seen": last_ts,
        "probes": [
            {"name": p["probe"], "status": p["status"], "value": p["value"]}
            for p in probes
        ],
    }


@app.get("/api/fleet", dependencies=[Depends(require_token)])
async def fleet():
    hosts = [_host_view(h) for h in db.all_hosts()]
    roles: dict[str, dict] = {}
    totals = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0}
    for h in hosts:
        totals[h["status"]] = totals.get(h["status"], 0) + 1
        r = roles.setdefault(h["role"], {"ok": 0, "warn": 0, "crit": 0, "unknown": 0, "total": 0})
        r[h["status"]] = r.get(h["status"], 0) + 1
        r["total"] += 1
    fleet_status = ("critical" if totals["crit"] else
                    "warning" if totals["warn"] else
                    "degraded" if totals["unknown"] and not (totals["ok"]) else
                    "healthy")
    return {
        "fleet_status": fleet_status,
        "totals": {**totals, "hosts": len(hosts)},
        "roles": roles,
        "hosts": hosts,
        "ts": int(time.time()),
    }


@app.get("/api/host/{hostname}", dependencies=[Depends(require_token)])
async def host_detail(hostname: str):
    hosts = {h["id"]: h for h in db.all_hosts()}
    if hostname not in hosts:
        raise HTTPException(status_code=404, detail="unknown host")
    view = _host_view(hosts[hostname])
    view["history"] = {
        p.name: db.probe_history(hostname, p.name, limit=60)
        for p in probes_for(hosts[hostname]["role"])
    }
    return view


@app.get("/api/incidents", dependencies=[Depends(require_token)])
async def incidents():
    """Advisor feed (Phase 2, read-only): live incidents + the vetted fix.

    Each known-failure incident carries the exact remediation command an
    operator should run. `needs-human` incidents are crit probes no playbook
    covers — surfaced honestly with no auto-fix. Pulse executes none of this.
    """
    books = {b.id: b for b in load_playbooks()}
    out = []
    for inc in db.open_incidents():
        kind = inc["kind"]
        pb = books.get(kind)
        if pb is not None:
            advisory = pb.as_dict()
        else:
            probe = kind.split(":", 1)[1] if ":" in kind else kind
            advisory = {
                "id": kind, "severity": "unknown", "role": inc["role"],
                "diagnose": (f"Probe '{probe}' is critical and no vetted playbook "
                             "covers this failure — needs human triage."),
                "command": "", "destructive": False, "verify": {}, "steps": [],
            }
        out.append({
            "id": inc["id"],
            "hostname": inc["host_id"],
            "ip": inc["ip"],
            "role": inc["role"],
            "status": inc["status"],          # open | needs-human
            "kind": kind,
            "opened_ts": inc["opened_ts"],
            "advisory": advisory,
        })
    # worst first: needs-human, then critical, then warning
    sev_rank = {"critical": 0, "warning": 1, "info": 2, "unknown": -1}
    out.sort(key=lambda i: (sev_rank.get(i["advisory"]["severity"], 3), -i["opened_ts"]))
    return {
        "count": len(out),
        "remediation_enabled": settings.remediation_enabled,
        "phase": "autoheal" if settings.remediation_enabled else "advisor",
        "governor": governor.status(),
        "incidents": out,
        "ts": int(time.time()),
    }


@app.post("/api/incidents/{incident_id}/approve", dependencies=[Depends(require_token)])
async def approve(incident_id: int, body: dict = Body(default={})):
    """Human-approved remediation (Phase 3). Governed, gated, audited.

    Default-deny: returns 403 unless PULSE_REMEDIATION_ENABLED is on, the
    blast-radius cap has headroom, and destructive fixes are explicitly
    confirmed. On ALLOW, runs the vetted playbook once with verify-or-rollback.
    """
    inc = db.get_incident(incident_id)
    if not inc or inc["status"] == "resolved":
        raise HTTPException(status_code=404, detail="unknown or already-resolved incident")

    books = {b.id: b for b in load_playbooks()}
    pb = books.get(inc["kind"])
    if pb is None:
        # needs-human incidents have no vetted fix — nothing to approve.
        raise HTTPException(status_code=422, detail="no vetted playbook for this incident (needs human triage)")

    approver = str(body.get("approver") or "operator")[:64]
    confirmed = bool(body.get("confirm_destructive", False))

    decision = governor.check(pb, autonomous=False, destructive_confirmed=confirmed)
    if not decision.allowed:
        db.audit(approver, "remediate_denied", target=inc["host_id"],
                 detail={"incident": incident_id, "playbook": pb.id, "reason": decision.reason})
        raise HTTPException(status_code=403, detail=decision.reason)

    host = remediator.host_by_name(inc["host_id"])
    if host is None:
        raise HTTPException(status_code=404, detail="host not in current inventory")
    if not host.has_credentials:
        raise HTTPException(status_code=409, detail="no SSH credentials for this host")

    result = await remediator.execute(inc, pb, host, approver)
    return {"incident": incident_id, "playbook": pb.id, "host": inc["host_id"], **result}


# ── AI copilot (Phase 4, local model only, advisory) ─────────────────────
# Heartbeat interval: how long we'll sit silent before emitting a keep-alive
# space. Must stay well under Cloudflare's 100s origin timeout.
_AI_HEARTBEAT = 8


async def _sse_like_stream(make_gen):
    """Bridge a blocking token generator to an async stream with heartbeats.

    `make_gen` is a zero-arg callable returning a *blocking* generator of text
    chunks (our urllib-based Ollama stream). We run it in a worker thread and
    pull chunks over a thread-safe queue. Two things defeat the Cloudflare 524
    here: (1) we emit a byte immediately so the origin's first byte lands in
    <1s, and (2) we emit a keep-alive space every few seconds while the model
    is still doing prompt-eval, so Cloudflare never sees a silent gap no matter
    how large the prompt is. Real tokens then stream continuously once they
    start. Heartbeat spaces are leading/whitespace only and trimmed client-side.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    DONE = object()

    def worker():
        try:
            for chunk in make_gen():
                loop.call_soon_threadsafe(q.put_nowait, chunk)
        except llm.LLMUnavailable as e:
            loop.call_soon_threadsafe(q.put_nowait, f"\n\n[copilot unavailable: {e}]")
        except Exception as e:  # noqa: BLE001 — surface, don't hang the stream
            loop.call_soon_threadsafe(q.put_nowait, f"\n\n[copilot error: {e}]")
        finally:
            loop.call_soon_threadsafe(q.put_nowait, DONE)

    threading.Thread(target=worker, daemon=True).start()
    yield " "  # immediate first byte → Cloudflare starts the response now
    while True:
        try:
            item = await asyncio.wait_for(q.get(), timeout=_AI_HEARTBEAT)
        except asyncio.TimeoutError:
            yield " "  # keep-alive during long prompt-eval
            continue
        if item is DONE:
            break
        yield item


@app.post("/api/ask", dependencies=[Depends(require_token)])
async def ask(body: dict = Body(default={})):
    """Free-form Q&A over the live fleet snapshot. Local model only, read-only.

    Returns 503 when no local model is configured (PULSE_LLM_URL unset) so the
    dashboard can degrade the tab gracefully. The model sees only non-secret
    operational data — never credentials — and can execute nothing.
    """
    if not llm.available():
        raise HTTPException(status_code=503, detail="AI copilot disabled: no local model configured (PULSE_LLM_URL)")
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    context = aicontext.fleet_context()
    db.audit("copilot", "ask", detail={"question": question[:500]})
    stream = _sse_like_stream(lambda: llm.stream_ask(question, context))
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8")


@app.post("/api/incidents/{incident_id}/analyze", dependencies=[Depends(require_token)])
async def analyze(incident_id: int):
    """AI triage for a needs-human incident. Advisory text only — never executes.

    Deliberately separate from the governed `/approve` path: this returns words
    for an operator, and there is no code path from here to the governor.
    """
    if not llm.available():
        raise HTTPException(status_code=503, detail="AI copilot disabled: no local model configured (PULSE_LLM_URL)")
    inc = db.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="unknown incident")
    context = aicontext.incident_context(inc)
    db.audit("copilot", "analyze", target=inc["host_id"], detail={"incident": incident_id})
    stream = _sse_like_stream(lambda: llm.stream_analyze(context))
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8")


# ── dashboard ────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "dashboard not built"}, status_code=500)
    return FileResponse(path)
