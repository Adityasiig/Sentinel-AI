"""Load the fleet inventory and resolve per-host SSH credentials.

Two supported sources, in priority order:

1. ``PULSE_FLEET_B64`` / ``PULSE_FLEET_JSON`` — a single injected secret holding
   the *entire* fleet, including per-host ``user`` + ``password``. This is the
   production path: the real fleet does not share one password per role, so
   creds are carried per host. Base64 form is preferred because VOIP-box
   passwords contain shell-hostile characters ($ ! % ] > < # { } * @) that get
   mangled when pasted raw into a Coolify env var.

2. ``PULSE_INVENTORY`` YAML (non-secret hosts) + per-role env creds — the
   original shared-password model, kept as a fallback for simple deployments.

Neither source is ever committed to git.
"""
from __future__ import annotations

import base64
import binascii
import json
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


def _read_fleet_blob() -> list[dict] | None:
    """Return the raw fleet records from the injected secret, or None if unset.

    Accepts a base64-wrapped JSON (``PULSE_FLEET_B64``) or plain JSON
    (``PULSE_FLEET_JSON``). Payload is a list of host dicts, each carrying its
    own ``user``/``password`` — that is what makes per-host creds work.
    """
    b64 = os.environ.get("PULSE_FLEET_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    else:
        raw = os.environ.get("PULSE_FLEET_JSON", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # allow either a bare list or {"hosts": [...]}
    if isinstance(data, dict):
        data = data.get("hosts", [])
    return data if isinstance(data, list) else None


def _hosts_from_blob(records: list[dict]) -> list[Host]:
    hosts: list[Host] = []
    for entry in records:
        role = str(entry.get("role", "")).upper()
        if role not in VALID_ROLES:
            continue
        hosts.append(Host(
            hostname=entry.get("hostname") or entry["ip"],
            ip=entry["ip"],
            role=role,
            ssh_port=int(entry.get("ssh_port", 22)),
            meta=entry.get("meta", {}) or {},
            ssh_user=entry.get("user", "root"),
            ssh_password=entry.get("password", ""),
        ))
    return hosts


def load_hosts(path: str | None = None) -> list[Host]:
    # Production path: full fleet (with per-host creds) from a single secret.
    records = _read_fleet_blob()
    if records is not None:
        return _hosts_from_blob(records)

    # Fallback: non-secret YAML + per-role env creds.
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
