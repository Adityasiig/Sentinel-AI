"""FreeSWITCH (IVG) probes — read-only.

Liveness + SIP registration health via fs_cli, with a plain process-check
fallback when fs_cli isn't usable over the SSH session.
"""
from __future__ import annotations

from .base import OK, WARN, CRIT, UNKNOWN, Probe, load_avg, proc_up, result


def _registrations(stdout: str, ok: bool) -> tuple[str, str]:
    # `fs_cli -x 'show registrations'` ends with e.g. "12 total." — pull the int.
    out = stdout.strip()
    if not out or "total" not in out:
        return result(UNKNOWN, out.splitlines()[-1] if out else "?")
    try:
        n = int(out.rsplit("total", 1)[0].strip().split()[-1])
    except (ValueError, IndexError):
        return result(UNKNOWN, "?")
    return result(OK if n > 0 else WARN, f"{n} regs")


def _profiles(stdout: str, ok: bool) -> tuple[str, str]:
    # `fs_cli -x 'sofia status'` lists profiles; RUNNING is healthy.
    out = stdout.strip()
    if not out:
        return result(UNKNOWN, "no fs_cli")
    running = out.upper().count("RUNNING")
    return result(OK if running > 0 else CRIT, f"{running} running")


PROBES = [
    Probe(
        name="freeswitch",
        command="pgrep -x freeswitch >/dev/null && echo up || echo down",
        evaluate=proc_up,
        description="FreeSWITCH process",
    ),
    Probe(
        name="sofia",
        command="fs_cli -x 'sofia status' 2>/dev/null || true",
        evaluate=_profiles,
        description="Sofia SIP profiles running",
    ),
    Probe(
        name="registrations",
        command="fs_cli -x 'show registrations' 2>/dev/null || true",
        evaluate=_registrations,
        description="Active SIP registrations",
    ),
    Probe(
        name="load",
        command="cat /proc/loadavg",
        evaluate=load_avg,
        description="1-minute load average",
    ),
]
