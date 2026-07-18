"""FastAPI surface — Phase 1 (read-only).

Serves the dashboard SPA and a small JSON API the SPA polls. Every /api route is
token-gated when PULSE_TOKEN is set. The prober background loop is started on
app startup so the fleet stays fresh even with no browser open (the SPA reads
cached state — Grafana-style stale-while-revalidate).
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, db, prober
from .config import settings
from .inventory import load_hosts
from .probes import probes_for, roll_up

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
    return {"status": "ok", "version": __version__}


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


# ── dashboard ────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "dashboard not built"}, status_code=500)
    return FileResponse(path)
