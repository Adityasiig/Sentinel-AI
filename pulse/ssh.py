"""Agentless SSH runner — concurrency-capped fan-out to the fleet.

Read-only by contract in Phase 1: callers only ever pass health-probe
one-liners. A single connection per host per sweep runs every probe for that
host, minimising session churn across 82 boxes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import asyncssh

from .config import settings
from .inventory import Host


@dataclass
class CommandResult:
    ok: bool
    stdout: str
    error: str = ""


class HostSession:
    """A live SSH connection to one host, used to run several probe commands."""

    def __init__(self, conn: asyncssh.SSHClientConnection):
        self._conn = conn

    async def run(self, command: str) -> CommandResult:
        try:
            r = await asyncio.wait_for(
                self._conn.run(command, check=False), timeout=settings.ssh_timeout
            )
            return CommandResult(ok=(r.exit_status == 0),
                                 stdout=(r.stdout or "").strip(),
                                 error=(r.stderr or "").strip())
        except asyncio.TimeoutError:
            return CommandResult(ok=False, stdout="", error="command timeout")
        except Exception as e:  # noqa: BLE001 — never let one probe kill the sweep
            return CommandResult(ok=False, stdout="", error=str(e))


def _connect_kwargs(host: Host) -> dict:
    kwargs = dict(
        host=host.ip,
        port=host.ssh_port,
        username=host.ssh_user,
        known_hosts=None,               # fleet hosts aren't in a known_hosts db
        connect_timeout=settings.ssh_timeout,
    )
    key = getattr(settings, "ssh_key", "")
    if key:
        kwargs["client_keys"] = [asyncssh.import_private_key(key)]
    else:
        # Password auth only. Without this, asyncssh first offers any local
        # agent/default identity keys; on a locked-down sshd that exhausts
        # MaxAuthTries ("Too many authentication failures") before it ever
        # reaches the password. Disable key + agent auth so we go straight to it.
        kwargs["password"] = host.ssh_password
        kwargs["client_keys"] = None
        kwargs["agent_path"] = None
        kwargs["preferred_auth"] = ("keyboard-interactive", "password")
    return kwargs


async def with_session(host: Host, work) -> object:
    """Open one connection to `host`, hand a HostSession to `work`, close cleanly.

    Returns whatever `work` returns, or raises the connection error to the caller
    (the prober catches it and marks the host unreachable).
    """
    async with asyncssh.connect(**_connect_kwargs(host)) as conn:
        return await work(HostSession(conn))


async def fan_out(hosts: list[Host], work, concurrency: int | None = None) -> dict:
    """Run `work(host, session)` across many hosts with a concurrency cap.

    `work` is an async callable taking (host, HostSession) and returning a value.
    Returns {hostname: {"ok": bool, "value": <work result> | None, "error": str}}.
    """
    sem = asyncio.Semaphore(concurrency or settings.ssh_concurrency)
    results: dict[str, dict] = {}

    async def _one(host: Host):
        async with sem:
            if not host.has_credentials:
                results[host.hostname] = {"ok": False, "value": None,
                                          "error": "no credentials for role"}
                return
            try:
                async def _wrapped(sess: HostSession):
                    return await work(host, sess)
                value = await with_session(host, _wrapped)
                results[host.hostname] = {"ok": True, "value": value, "error": ""}
            except Exception as e:  # noqa: BLE001
                results[host.hostname] = {"ok": False, "value": None, "error": str(e)}

    await asyncio.gather(*(_one(h) for h in hosts))
    return results
