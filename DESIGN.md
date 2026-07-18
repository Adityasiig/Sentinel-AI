# Pulse — Agentless Self-Healing Control Plane for a Multi-Vendor VoIP Fleet

> One control node SSHes into 82 production switches (37 FreeSWITCH · 25 OpenSIPS ·
> 20 VOS3000), runs role-aware health probes, and auto-remediates *known* failures
> behind a safety governor. An LLM assists only on the *unknown* ones.

**Status:** DESIGN (paper phase — nothing built yet)
**Deploy target:** GitHub (private) → Coolify (Dockerfile build pack) → `pulse.crownitsolution.com`
**Author:** Aditya

---

## 1. Why this exists

The fleet has recurring, *known* failure modes that page a human at 3am for a fix
that's mechanical:

- **VOS3000:** `/` partition fills with CDRs → MySQL `ENOSPC` → port `1355` stops
  binding → "server timeout". Fix is a known script (archive CDRs to `/home`).
- **OpenSIPS:** a rogue SIP daemon grabs `5060` → OpenSIPS won't restart clean.
- **FreeSWITCH:** a sofia profile wedges or a process dies → registrations drop.

Pulse turns each of these from a pager into a policy. It never guesses on a
production switch — known failures run vetted playbooks, novel ones stop and ask.

---

## 2. Core principles

1. **Agentless.** No daemon on 82 CentOS 7 / Debian boxes. One control node, pure SSH.
2. **Read-only by default.** Every deployment starts as an observer. Write access is
   opt-in, per-playbook, and gated.
3. **Known ≠ unknown.** Detection maps to a *vetted* playbook or it escalates to a
   human (optionally with an LLM-drafted suggestion). Unknown fixes never auto-run.
4. **Blast radius is capped.** The governor refuses to act on more than N boxes in a
   window. A bad probe can't trigger a fleet-wide storm.
5. **Everything is audited.** Every probe, decision, command, before/after, and
   approver is written to an immutable log.
6. **Secrets never touch git.** Fleet credentials are injected at runtime only.

---

## 3. Architecture

```
                         ┌──────────────────────────────────────────┐
                         │            Pulse control node             │
                         │        (container, deployed via Coolify)  │
                         │                                           │
  inventory.yaml ──────► │  Inventory  ─┐                            │
  (roles, ports,         │              ▼                            │
   NO secrets)           │           Prober ──► SQLite (state)       │
                         │              │         ▲                  │
  Coolify secrets ─────► │              ▼         │                  │
  (SSH creds, per env)   │          Diagnoser ────┤                  │
                         │              │         │                  │
                         │              ▼         │                  │
                         │          Governor ──► Remediator          │
                         │              │              │             │
                         │              ▼              ▼             │
                         │   FastAPI + Dashboard    SSH fan-out ─────┼──► 82 boxes :22
                         │   (auth-gated)                            │
                         └──────────────────────────────────────────┘
                                        │
                              (optional) Ollama on Helios .62 — LLM for unknowns
```

### Components
| Module | Responsibility |
|---|---|
| `inventory` | Load host registry (YAML, non-secret) + merge runtime secrets → 82 host objects tagged IVG/OPS/VOSS |
| `prober` | Async scheduler; runs role-specific probe sets on a cadence; concurrency-capped SSH fan-out; writes `probe_runs` |
| `diagnoser` | Map a failed probe → a playbook (`detect` rule match); if none → open an `incident` flagged `needs-human` |
| `remediator` | Execute a playbook's `remediate` steps; capture before/after; run `verify`; `rollback` on failure |
| `governor` | Policy gate: dry-run default, approval queue, blast-radius cap, destructive-op gate, audit writer |
| `api` | FastAPI: fleet health JSON, incident list, approval endpoints, audit log |
| `dashboard` | Static SPA: fleet map by role, red/amber/green, incident + approval UI |
| `llm` (optional) | Draft a suggested fix for `needs-human` incidents via local Ollama — never auto-executes |

---

## 4. Data model (SQLite, on a persistent volume)

```
hosts(id, hostname, ip, role, ssh_port, meta_json)
probe_runs(id, host_id, probe, status, value, ts)          -- ok|warn|crit
incidents(id, host_id, kind, status, opened_ts, closed_ts) -- open|remediating|resolved|needs-human
actions(id, incident_id, playbook, cmd, destructive,
        dry_run, approved_by, result, before, after, ts)
audit(id, actor, verb, target, detail_json, ts)            -- append-only
```

---

## 5. Probes (per role)

| Role | Probes |
|---|---|
| **VOSS** (VOS3000) | `root_disk_pct`, `mysql_up`, `port_1355_bound`, `web_admin_bound`, `callservice_running` |
| **OPS** (OpenSIPS) | `opensips_proc`, `sip_5060_udp`, `dispatcher_reachable`, `no_rogue_sip_daemon` |
| **IVG** (FreeSWITCH) | `fs_running`, `sofia_profiles_up`, `registration_count`, `load_avg` |

Probes are read-only shell one-liners over SSH. Each returns `ok|warn|crit` + a value.

---

## 6. Playbook format (declarative YAML — vetted, versioned in git)

```yaml
id: vos-disk-full
role: VOSS
severity: critical
detect:
  probe: root_disk_pct
  op: ">"
  threshold: 90
diagnose: >
  Root partition near full. MySQL will hit ENOSPC and VOS3000 stops binding 1355.
remediate:
  - name: archive CDRs older than 60d to /home
    destructive: true            # => requires approval unless playbook is auto-approved
    cmd: |
      mkdir -p /home/mysql_archive/vos3000
      # move e_cdr_* older than 60 days to /home, then let MySQL reconnect
      ...
verify:
  probe: port_1355_bound
  expect: true
rollback:
  - name: move archived CDRs back
    cmd: "mv /home/mysql_archive/vos3000/e_cdr_* /var/lib/mysql/vos3000/"
```

Known playbooks at launch: `vos-disk-full`, `ops-rogue-sip-daemon`, `fs-profile-wedged`.

---

## 7. Safety governor (the part that makes it *senior*, not a toy)

- **Dry-run default** — every remediation is simulated (diff/plan) before it can run.
- **Approval queue** — destructive steps require an approver (dashboard click) unless
  the playbook is explicitly marked `auto_approve: true` (only proven ones).
- **Blast-radius cap** — max K remediations per T minutes across the fleet; hard stop.
- **Concurrency cap on probes** — never SSH-storm 82 boxes at once.
- **Verify-or-rollback** — if `verify` fails post-fix, auto-run `rollback`, reopen incident.
- **Immutable audit** — actor, command, before/after, approver, timestamp for everything.

---

## 8. Secrets — the design decision GitHub + Coolify forces

**`crown_server_creds.md` and all passwords/keys NEVER enter the repo.**

- Repo ships `inventory.example.yaml` (hosts, roles, ports — no secrets) and
  `credentials.example.yaml` (structure only).
- `.gitignore` excludes: real creds, `*.db`, `.env`, `pulse.sqlite`.
- Runtime injection via **Coolify environment variables / secret store**:
  - `IVG_SSH_PASSWORD` (shared across ~34 FreeSWITCH boxes)
  - `OPS_SSH_PASSWORD` (shared across OpenSIPS boxes)
  - `VOSS_CREDS_JSON` (the 20 unique-per-box VOS3000 passwords, as a JSON blob secret)
- **Better (Phase 2+):** provision a dedicated least-privilege `pulse` SSH *key* on all
  boxes and drop password auth entirely. Container holds only the private key (Coolify secret).

The container becomes a crown-jewel target (it can SSH into 82 prod switches), so:
- Dashboard is **auth-gated** (login/token) + ideally IP allowlist.
- Secrets live in env/memory only — **never logged, never in the image**.
- Least-privilege target user; audit every session.

---

## 9. Tech stack (Coolify-friendly)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | matches existing tooling (Sentinel, doctor) |
| Web/API | FastAPI + uvicorn | async, clean JSON API, small |
| SSH | `asyncssh` | async fan-out to 82 boxes, key or password auth |
| State | SQLite on a persistent volume | zero external DB; survives redeploys |
| Scheduler | asyncio loop (no Celery) | keep it single-container |
| Dashboard | static SPA (vanilla, like ui.html) | no build step |
| Container | Dockerfile (python:3.12-slim, non-root uid) | Coolify build pack |

---

## 10. Repo layout

```
pulse/
  README.md
  DESIGN.md                 <- this file
  Dockerfile
  .dockerignore
  .gitignore                # creds, *.db, .env
  requirements.txt
  inventory.example.yaml
  credentials.example.yaml
  pulse/
    __init__.py
    inventory.py
    db.py
    prober.py
    probes/{vos.py, opensips.py, freeswitch.py}
    diagnoser.py
    remediator.py
    governor.py
    playbooks/*.yaml
    api.py
    llm.py                  # optional, Phase 3
  web/                      # static dashboard
```

---

## 11. Coolify deployment plan

1. **Private** GitHub repo (infra tooling — not public). Coolify connects via GitHub App / deploy key.
2. Dockerfile build pack, expose `8080`.
3. Secrets as Coolify env vars (§8).
4. **Persistent volume** mounted at `/data` for `pulse.sqlite` (state survives redeploys).
5. Behind Coolify Traefik → TLS + `pulse.crownitsolution.com`.
6. **Pre-flight (Phase 0):** confirm the Coolify host can reach the 82 boxes on `:22`.
   If Coolify's network can't SSH the fleet, the control node must run where it can
   (this becomes a hard placement constraint, not an afterthought).

---

## 12. Phased rollout

| Phase | Capability | Write access | Risk |
|---|---|---|---|
| **0** | SSH reachability + inventory load | none | none |
| **1 — Observer** | probes + live fleet health dashboard | **read-only** | none |
| **2 — Advisor** | diagnose + show exact fix command, human clicks run | manual only | low |
| **3 — Auto-heal** | proven playbooks (e.g. `vos-disk-full`) run behind governor | gated auto | managed |

Ship Phase 1 first. It's useful on its own and carries zero risk to production calls.

---

## 13. Open decisions (need Aditya's call before build)

- **D1 — Repo visibility:** private (recommended) vs public.
- **D2 — Secrets mechanism:** Coolify env vars now, or invest in a dedicated `pulse`
  SSH key across the fleet up front.
- **D3 — Coolify network reach:** does the Coolify host have SSH line-of-sight to all
  82 boxes? (Phase-0 check — decides placement.)
- **D4 — Dashboard auth:** token gate vs full login + IP allowlist.
