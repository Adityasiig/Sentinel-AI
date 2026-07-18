"""OpenSIPS (OPS) probes — read-only.

Watches for the classic failure: a rogue SIP daemon (freeswitch/asterisk/
kamailio) grabbing :5060 so OpenSIPS won't restart clean, plus core liveness.
"""
from __future__ import annotations

from .base import OK, CRIT, Probe, load_avg, proc_up, result, yes_bound


def _no_rogue(stdout: str, ok: bool) -> tuple[str, str]:
    # probe prints the name of any conflicting daemon, or "clean"
    out = stdout.strip()
    if out and out != "clean":
        return result(CRIT, f"rogue:{out}")
    return result(OK, "clean")


PROBES = [
    Probe(
        name="opensips",
        command="pgrep -x opensips >/dev/null && echo up || echo down",
        evaluate=proc_up,
        description="OpenSIPS process",
    ),
    Probe(
        name="sip_5060",
        command="ss -lunH 'sport = :5060' | grep -q . && echo yes || echo no",
        evaluate=yes_bound,
        description="SIP UDP/5060 bound",
    ),
    Probe(
        name="rogue_sip",
        command=("for d in freeswitch asterisk kamailio; do "
                 "pgrep -x $d >/dev/null && echo $d && exit 0; done; echo clean"),
        evaluate=_no_rogue,
        description="Conflicting SIP daemon on the box",
    ),
    Probe(
        name="load",
        command="cat /proc/loadavg",
        evaluate=load_avg,
        description="1-minute load average",
    ),
]
