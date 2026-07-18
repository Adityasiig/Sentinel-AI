"""Safety governor — the policy gate that stands between a proposed fix and a
command actually running on a production box.

Every remediation must clear this gate first. It is deliberately conservative:
default-deny, and it refuses on the first failed check. The checks, in order:

1. Remediation must be globally enabled (`PULSE_REMEDIATION_ENABLED`). Off = no
   write ever reaches the fleet, no matter what the UI sends.
2. Blast radius: no more than `blast_radius` real executions in `blast_window`
   seconds across the whole fleet — a bug or a bad sweep can't fan out.
3. Destructive playbooks require an explicit per-approval confirmation.
4. Autonomous (no-human) execution requires `PULSE_AUTOHEAL` on *and* an
   `auto_approve` playbook. Absent either, a human must approve.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .config import settings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason}


def check(playbook, *, autonomous: bool = False, destructive_confirmed: bool = False) -> Decision:
    """Return whether `playbook` may execute now. `autonomous` = no human click."""
    if not settings.remediation_enabled:
        return Decision(False, "remediation is globally disabled (PULSE_REMEDIATION_ENABLED)")

    recent = db.count_recent_executions(settings.blast_window)
    if recent >= settings.blast_radius:
        return Decision(
            False,
            f"blast-radius cap reached: {recent}/{settings.blast_radius} "
            f"executions in the last {settings.blast_window}s",
        )

    if playbook.destructive and not destructive_confirmed:
        return Decision(False, "destructive remediation requires explicit confirmation")

    if autonomous:
        if not settings.autoheal_enabled:
            return Decision(False, "autonomous auto-heal is disabled (PULSE_AUTOHEAL); needs a human approval")
        if not getattr(playbook, "auto_approve", False):
            return Decision(False, f"playbook '{playbook.id}' is not marked auto_approve")

    return Decision(True, "cleared")


def status() -> dict:
    """Governor state for the API/dashboard (no secrets)."""
    return {
        "remediation_enabled": settings.remediation_enabled,
        "autoheal_enabled": settings.autoheal_enabled,
        "blast_radius": settings.blast_radius,
        "blast_window": settings.blast_window,
        "recent_executions": db.count_recent_executions(settings.blast_window),
    }
