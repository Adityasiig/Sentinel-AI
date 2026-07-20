"""Unified logs — Phase 1 read-only, on-demand.

When an operator opens a host, Pulse SSHes in and tails the tail of that box's
role-appropriate log over the *same* agentless mechanism the probes use. Nothing
is stored: this is a live `tail`, not a collector. That keeps the read-only
guarantee intact (no writes to the fleet, no DB growth, no extra per-sweep SSH
load) and the pane is always current.

Log location varies per vendor and per box, so we don't hardcode one path. For
each role we try an ordered list of candidate files (first that exists wins) and
fall back to `journalctl` for the service unit. Everything issued to the box is
strictly read-only: `ls`, `tail`, `journalctl`, `grep`.

Security note: the optional search filter is attacker-controllable text that
ends up in a remote shell command, so it is (1) rejected if it contains a
single quote or control chars and (2) passed to `grep -F` as a single-quoted
fixed string — never interpolated into the command in a way that can break out.
"""
from __future__ import annotations

import re
import shlex

from . import ssh
from .inventory import Host, load_hosts

# Ordered candidate log files per role. First one that exists on the box wins.
_CANDIDATES: dict[str, list[str]] = {
    "IVG": [
        "/usr/local/freeswitch/log/freeswitch.log",
        "/var/log/freeswitch/freeswitch.log",
        "/var/log/freeswitch.log",
    ],
    "OPS": [
        "/var/log/opensips.log",
        "/var/log/opensips/opensips.log",
    ],
    # VOSS logs live under /home/kunshi/<svc>/log/*.log — resolved by glob below.
    "VOSS": [],
}

# systemd unit to fall back to when no candidate file is present.
_JOURNAL_UNIT: dict[str, str] = {"IVG": "freeswitch", "OPS": "opensips", "VOSS": ""}

LINES_MIN, LINES_MAX, LINES_DEFAULT = 50, 2000, 500

# a search filter may only contain these — keeps it a plain substring match
_SAFE_FILTER = re.compile(r"^[\w .:/@=%,\-\[\]()#]*$")


class LogError(RuntimeError):
    pass


def _clamp_lines(n: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return LINES_DEFAULT
    return max(LINES_MIN, min(LINES_MAX, n))


def _safe_filter(grep: str) -> str:
    grep = (grep or "").strip()
    if not grep:
        return ""
    if len(grep) > 120 or not _SAFE_FILTER.match(grep):
        raise LogError("invalid search filter (allowed: letters, digits and . : / @ = % , - [ ] ( ) #)")
    return grep


def build_command(role: str, lines: int, grep: str = "") -> str:
    """Build the read-only shell one-liner that emits the last `lines` log lines.

    Resolves the log source on the box itself so a per-host path difference
    doesn't require Pulse to know it in advance.
    """
    role = role.upper()
    n = _clamp_lines(lines)
    flt = _safe_filter(grep)

    if role == "VOSS":
        # newest .log across every VOS3000 service's log dir
        pick = 'f="$(ls -t /home/kunshi/*/log/*.log 2>/dev/null | head -n1)"'
    else:
        cands = " ".join(shlex.quote(p) for p in _CANDIDATES.get(role, []))
        pick = f'f=""; for c in {cands}; do [ -f "$c" ] && f="$c" && break; done'

    unit = _JOURNAL_UNIT.get(role, "")
    tail = f'tail -n {n} "$f"'
    if unit:
        fallback = f'journalctl -u {shlex.quote(unit)} -n {n} --no-pager 2>/dev/null'
        body = f'if [ -n "$f" ]; then {tail}; else {fallback}; fi'
    else:
        body = f'if [ -n "$f" ]; then {tail}; else echo "__PULSE_NO_LOG__"; fi'

    if flt:
        body = f'({body}) | grep -F -- {shlex.quote(flt)} | tail -n {n}'
    return f'{pick}; {body}'


# severity classification for colouring the pane (best-effort, case-insensitive)
_SEV = [
    ("crit", re.compile(r"\b(crit|critical|fatal|emerg|alert|panic)\b", re.I)),
    ("err",  re.compile(r"\b(err|error|fail|failed|failure|denied|refused)\b", re.I)),
    ("warn", re.compile(r"\b(warn|warning)\b", re.I)),
    ("notice", re.compile(r"\b(notice)\b", re.I)),
]


def classify(line: str) -> str:
    for sev, rx in _SEV:
        if rx.search(line):
            return sev
    return "info"


def host_by_name(hostname: str) -> Host | None:
    for h in load_hosts():
        if h.hostname == hostname:
            return h
    return None


async def fetch(hostname: str, lines: int = LINES_DEFAULT, grep: str = "") -> dict:
    """Tail one host's log over a fresh SSH session. Read-only.

    Returns {"hostname", "role", "source", "lines":[{n,sev,text}], "truncated"}.
    Raises LogError with a caller-mappable message on unknown host / no creds /
    unreachable / no log found.
    """
    host = host_by_name(hostname)
    if host is None:
        raise LogError("unknown host")
    if not host.has_credentials:
        raise LogError("no SSH credentials for this host's role")

    cmd = build_command(host.role, lines, grep)

    async def _work(sess):
        # give tailing a little more room than a probe one-liner
        return await sess.run(cmd, timeout=20)

    try:
        result = await ssh.with_session(host, _work)
    except Exception as e:  # noqa: BLE001 — surface as a clean API error
        raise LogError(f"unreachable: {e}") from e

    out = (result.stdout or "").strip()
    if not out or out == "__PULSE_NO_LOG__":
        return {"hostname": hostname, "role": host.role, "source": "none",
                "lines": [], "truncated": False,
                "note": "no readable log file or journal found on this box"}

    raw = out.splitlines()
    n = _clamp_lines(lines)
    truncated = len(raw) > n
    raw = raw[-n:]
    parsed = [{"n": i + 1, "sev": classify(ln), "text": ln} for i, ln in enumerate(raw)]
    return {"hostname": hostname, "role": host.role, "source": "tail",
            "lines": parsed, "truncated": truncated}
