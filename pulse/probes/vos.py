"""VOS3000 (VOSS) probes — read-only.

Targets the known failure chain: CDRs fill `/` -> MySQL ENOSPC -> port 1355
stops binding -> "server timeout". Each probe here is a symptom in that chain.
"""
from __future__ import annotations

from .base import OK, CRIT, Probe, disk_pct, proc_up, result, yes_bound


def _mysql_up(stdout: str, ok: bool) -> tuple[str, str]:
    return proc_up(stdout, ok)


def _callservice(stdout: str, ok: bool) -> tuple[str, str]:
    # On cracked boxes callservice can be legitimately down; report WARN not CRIT.
    up = stdout.strip().endswith("up")
    return result(OK if up else "warn", "up" if up else "down")


PROBES = [
    Probe(
        name="root_disk",
        command="df -P / | awk 'NR==2{print $5}' | tr -d %",
        evaluate=disk_pct,
        description="Root partition usage (fills with CDRs → MySQL ENOSPC)",
    ),
    Probe(
        name="mysql",
        command="pgrep -x mysqld >/dev/null && echo up || echo down",
        evaluate=_mysql_up,
        description="MySQL/mysqld process",
    ),
    Probe(
        name="gui_1355",
        command="ss -tlnH 'sport = :1355' | grep -q . && echo yes || echo no",
        evaluate=yes_bound,
        description="VOS3000 desktop-client port 1355 bound",
    ),
    Probe(
        name="webserver",
        command="pgrep -f webserverd >/dev/null && echo up || echo down",
        evaluate=proc_up,
        description="Tomcat webserverd (web admin)",
    ),
    Probe(
        name="callservice",
        command="pgrep -f callservice >/dev/null && echo up || echo down",
        evaluate=_callservice,
        description="Call routing service (WARN if down — cracked-box dependent)",
    ),
]
