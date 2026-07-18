"""Probe abstraction.

A Probe is a read-only remote command plus a pure function that turns its raw
output into (status, value). Probes NEVER mutate the target — that's the whole
point of Phase 1. Keeping them declarative also makes them trivial to unit-test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"


@dataclass(frozen=True)
class Probe:
    name: str
    command: str                                   # read-only shell one-liner
    evaluate: Callable[[str, bool], tuple[str, str]]  # (stdout, ok) -> (status, value)
    description: str = ""


def result(status: str, value) -> tuple[str, str]:
    return status, str(value)


# ── reusable evaluators ──────────────────────────────────────────────────
def proc_up(stdout: str, ok: bool) -> tuple[str, str]:
    """`up`/`down` marker from a pgrep-style probe."""
    up = stdout.strip().endswith("up")
    return result(OK if up else CRIT, "up" if up else "down")


def yes_bound(stdout: str, ok: bool) -> tuple[str, str]:
    """`yes`/`no` marker for a port-bound check."""
    bound = stdout.strip().endswith("yes")
    return result(OK if bound else CRIT, "bound" if bound else "not-bound")


def disk_pct(stdout: str, ok: bool) -> tuple[str, str]:
    try:
        pct = int(stdout.strip().split()[-1])
    except (ValueError, IndexError):
        return result(UNKNOWN, stdout.strip() or "?")
    status = CRIT if pct >= 90 else WARN if pct >= 80 else OK
    return result(status, f"{pct}%")


def load_avg(stdout: str, ok: bool, warn: float = 16.0, crit: float = 32.0) -> tuple[str, str]:
    try:
        la = float(stdout.strip().split()[0])
    except (ValueError, IndexError):
        return result(UNKNOWN, stdout.strip() or "?")
    status = CRIT if la >= crit else WARN if la >= warn else OK
    return result(status, f"{la:.2f}")
