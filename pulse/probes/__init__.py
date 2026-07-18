"""Probe registry — maps a role to its ordered set of read-only probes."""
from __future__ import annotations

from .base import CRIT, OK, UNKNOWN, WARN, Probe
from . import freeswitch, opensips, vos

REGISTRY: dict[str, list[Probe]] = {
    "IVG": freeswitch.PROBES,
    "OPS": opensips.PROBES,
    "VOSS": vos.PROBES,
}

# Precedence for rolling a host's many probe statuses into one overall status.
_RANK = {CRIT: 3, WARN: 2, UNKNOWN: 1, OK: 0}


def probes_for(role: str) -> list[Probe]:
    return REGISTRY.get(role.upper(), [])


def roll_up(statuses: list[str]) -> str:
    """Worst-wins, but an all-unknown host is 'unknown' (unreachable), not 'ok'."""
    if not statuses:
        return UNKNOWN
    if all(s == UNKNOWN for s in statuses):
        return UNKNOWN
    return max((s for s in statuses if s != UNKNOWN), key=lambda s: _RANK[s], default=UNKNOWN)
