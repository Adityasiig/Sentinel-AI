"""VOS3000 (VOSS) probes — read-only.

Targets the known failure chain: CDRs fill `/` -> MySQL ENOSPC -> port 1355
stops binding -> "server timeout". Each probe here is a symptom in that chain.
"""
from __future__ import annotations

from .base import OK, WARN, CRIT, Probe, disk_pct, proc_up, result, yes_bound


def _mysql_up(stdout: str, ok: bool) -> tuple[str, str]:
    return proc_up(stdout, ok)


def _callservice(stdout: str, ok: bool) -> tuple[str, str]:
    # On cracked boxes callservice can be legitimately down; report WARN not CRIT.
    up = stdout.strip().endswith("up")
    return result(OK if up else "warn", "up" if up else "down")


def _gui_1355(stdout: str, ok: bool) -> tuple[str, str]:
    # Port 1355 is the *desktop client* endpoint. Our fleet is cracked VOS3000
    # where the desktop client is incompatible and 1355 is expected to stay
    # closed while web admin (webserverd) serves fine — verified across the
    # fleet. So an unbound 1355 is WARN, not CRIT: red is reserved for the real
    # outage chain (disk-full -> mysql ENOSPC -> webserverd down).
    bound = stdout.strip().endswith("yes")
    return result(OK if bound else WARN, "bound" if bound else "not-bound")


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
        evaluate=_gui_1355,
        description="VOS3000 desktop-client port 1355 (WARN if unbound — expected on cracked boxes)",
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
