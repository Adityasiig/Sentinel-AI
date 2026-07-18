# Pulse

**Agentless observability & self-healing control plane for a multi-vendor VoIP fleet.**

One control node SSHes into 82 production switches — 37 FreeSWITCH, 25 OpenSIPS,
20 VOS3000 — runs role-aware health probes on a cadence, and surfaces the whole
fleet's health on a single live dashboard. No agent is installed on any target
box. Phase 1 (this release) is **strictly read-only**.

```
┌────────────┐   SSH (read-only)   ┌──────────────────────────────┐
│   Pulse    │────────────────────▶│  82 boxes                    │
│  control   │  role-aware probes  │  IVG  · 37 FreeSWITCH        │
│   node     │◀────────────────────│  OPS  · 25 OpenSIPS         │
└────────────┘   cached state       │  VOSS · 20 VOS3000          │
      │                             └──────────────────────────────┘
      ▼
  live dashboard (stale-while-revalidate, polls cached state)
```

## Why

The fleet has recurring, *mechanical* failure modes that page a human at 3am:

| Role | Known failure | Fix (later phase) |
|------|---------------|-------------------|
| VOS3000 | `/` fills with CDRs → MySQL `ENOSPC` → port `1355` stops binding | archive CDRs to `/home` |
| OpenSIPS | rogue SIP daemon grabs `5060` → won't restart clean | reclaim the port |
| FreeSWITCH | sofia profile wedges / process dies → registrations drop | restart profile |

Pulse turns each from a pager into a policy. Phase 1 just *sees* the fleet;
remediation is gated behind a safety governor in later phases and ships disabled.

## Design principles

1. **Agentless** — no daemon on 82 CentOS 7 / Debian boxes, pure SSH.
2. **Read-only by default** — Phase 1 has zero write paths to production.
3. **Known ≠ unknown** — future auto-heal only runs *vetted* playbooks; novel
   failures stop and ask a human.
4. **Blast radius capped** — the governor refuses to act on more than N boxes/window.
5. **Everything audited** — every probe and decision written to an immutable log.
6. **Secrets never touch git** — fleet credentials injected at runtime only.

## Architecture

```
pulse/
├── config.py        # frozen Settings, loaded from env (no secrets in code)
├── db.py            # SQLite (WAL) state store — hosts, probe_runs, incidents, audit
├── inventory.py     # non-secret host list (roles: IVG/OPS/VOSS) + cred resolution
├── ssh.py           # asyncssh runner — one session per host, concurrency-capped fan-out
├── prober.py        # background heartbeat: sweep the fleet every PROBE_INTERVAL s
├── probes/
│   ├── base.py      # OK/WARN/CRIT/UNKNOWN, Probe dataclass, reusable evaluators
│   ├── freeswitch.py# IVG probes  (process, sofia, registrations, load)
│   ├── opensips.py  # OPS probes  (process, 5060, rogue-daemon, load)
│   └── vos.py       # VOSS probes (disk, mysql, 1355, webserver, callservice)
└── api.py           # FastAPI — /api/fleet, /api/host/{h}, token-gated, serves SPA
web/
└── index.html       # premium animated dashboard, zero build step
```

- **Stale-while-revalidate:** the prober keeps a fresh cache in SQLite; the SPA
  polls `/api/fleet` every 10s and renders cached state — no request ever blocks
  on a live SSH sweep.
- **Unreachable ≠ healthy:** a host that fails to answer is recorded as `unknown`
  across all its probes, never left showing stale green.

## Configuration (all via environment)

| Var | Default | Purpose |
|-----|---------|---------|
| `PULSE_PORT` | `8080` | HTTP listen port |
| `PULSE_TOKEN` | *(unset)* | if set, every `/api` route requires `X-Pulse-Token` |
| `PULSE_INVENTORY` | `inventory.yaml` | non-secret host list |
| `PULSE_DB_PATH` | `/data/pulse.sqlite` | SQLite state (WAL) |
| `PULSE_PROBE_INTERVAL` | `60` | seconds between fleet sweeps |
| `PULSE_SSH_CONCURRENCY` | `10` | max simultaneous SSH sessions |
| `PULSE_SSH_TIMEOUT` | `12` | per-command timeout (s) |
| `PULSE_REMEDIATION_ENABLED` | `false` | **hard-off in Phase 1** |

**Secrets (never committed):**

| Var | Purpose |
|-----|---------|
| `IVG_SSH_USER` / `IVG_SSH_PASSWORD` | shared creds for FreeSWITCH boxes |
| `OPS_SSH_USER` / `OPS_SSH_PASSWORD` | shared creds for OpenSIPS boxes |
| `VOSS_SSH_USER` / `VOSS_CREDS_JSON` | VOS3000 per-host password map (JSON) |
| `PULSE_SSH_KEY` | optional key path (used instead of passwords if set) |

## Run locally

```bash
pip install -r requirements.txt
cp inventory.example.yaml inventory.yaml    # add a few reachable hosts
export IVG_SSH_USER=root IVG_SSH_PASSWORD=...   # etc. per role
export PULSE_TOKEN=$(openssl rand -hex 16)
uvicorn pulse.api:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`, paste the token into the gate. First sweep lands
within `PROBE_INTERVAL` seconds.

## Deploy (Coolify)

Private GitHub repo → Coolify **Dockerfile** build pack → domain
`pulse.crownitsolution.com` (Let's Encrypt TLS). Inject the secrets above as
Coolify environment variables; mount a volume at `/data` for the SQLite store.
The Coolify host must have SSH reachability to the fleet on `:22`.

## Roadmap

- **Phase 1 — Observer** *(this release)* — read-only fleet health + dashboard.
- **Phase 2 — Advisor** — map probes to incidents, draft fixes (LLM on unknowns), no auto-run.
- **Phase 3 — Auto-heal** — vetted YAML playbooks behind the safety governor
  (dry-run default, approval queue, blast-radius cap, verify-or-rollback).

See [`DESIGN.md`](DESIGN.md) for the full architecture.
