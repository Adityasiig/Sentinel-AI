"""Load the non-secret host inventory and merge in runtime credentials.

The YAML never contains secrets. Credentials come from `settings.creds`
(populated from env). This is where a host object becomes "connectable".
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

from .config import settings

VALID_ROLES = {"IVG", "OPS", "VOSS"}


@dataclass
class Host:
    hostname: str
    ip: str
    role: str
    ssh_port: int
    meta: dict

    # resolved at load from settings.creds
    ssh_user: str = "root"
    ssh_password: str = ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.ssh_password) or bool(getattr(settings, "ssh_key", ""))

    def as_dict(self) -> dict:
        return {
            "hostname": self.hostname, "ip": self.ip, "role": self.role,
            "ssh_port": self.ssh_port, "meta": self.meta,
        }


def _resolve_creds(role: str, ip: str) -> tuple[str, str]:
    c = settings.creds.get(role, {})
    user = c.get("user", "root")
    if role == "VOSS":
        return user, c.get("per_host", {}).get(ip, "")
    return user, c.get("password", "")


def load_hosts(path: str | None = None) -> list[Host]:
    path = path or settings.inventory_path
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        doc = yaml.safe_load(f) or {}

    default_port = (doc.get("defaults") or {}).get("ssh_port", 22)
    hosts: list[Host] = []
    for entry in doc.get("hosts", []) or []:
        role = str(entry.get("role", "")).upper()
        if role not in VALID_ROLES:
            continue  # skip malformed rows rather than crash the sweep
        user, pwd = _resolve_creds(role, entry["ip"])
        hosts.append(Host(
            hostname=entry["hostname"],
            ip=entry["ip"],
            role=role,
            ssh_port=int(entry.get("ssh_port", default_port)),
            meta=entry.get("meta", {}) or {},
            ssh_user=user,
            ssh_password=pwd,
        ))
    return hosts
