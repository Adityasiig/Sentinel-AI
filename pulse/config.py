"""Runtime configuration — everything sensitive comes from the environment.

Nothing here reads a committed secret. Fleet credentials arrive as env vars
(Coolify secrets in production); inventory arrives as a non-secret YAML path.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # ── web ──────────────────────────────────────────────────────────────
    host: str = os.environ.get("PULSE_HOST", "0.0.0.0")
    port: int = _int("PULSE_PORT", 8080)
    # Dashboard/API token gate. Unset => open (fine for localhost only).
    token: str = os.environ.get("PULSE_TOKEN", "").strip()

    # ── inventory + state ────────────────────────────────────────────────
    inventory_path: str = os.environ.get("PULSE_INVENTORY", "inventory.yaml")
    db_path: str = os.environ.get("PULSE_DB_PATH", "/data/pulse.sqlite")

    # ── probing cadence + safety ─────────────────────────────────────────
    probe_interval: int = _int("PULSE_PROBE_INTERVAL", 60)      # seconds between sweeps
    ssh_concurrency: int = _int("PULSE_SSH_CONCURRENCY", 10)    # max parallel SSH sessions
    ssh_timeout: int = _int("PULSE_SSH_TIMEOUT", 12)            # per-host connect+run budget
    # Read-only until explicitly flipped. Phase 1 keeps this False, hard.
    remediation_enabled: bool = os.environ.get("PULSE_REMEDIATION_ENABLED", "").lower() in ("1", "true", "yes")

    # ── credentials (injected; never committed) ──────────────────────────
    # Per-role SSH users + shared passwords for IVG/OPS, per-box JSON for VOSS.
    creds: dict = field(default_factory=dict)

    @staticmethod
    def load() -> "Settings":
        s = Settings()
        # populate creds after construction (frozen dataclass -> object.__setattr__)
        creds = {
            "IVG": {
                "user": os.environ.get("IVG_SSH_USER", "root"),
                "password": os.environ.get("IVG_SSH_PASSWORD", ""),
            },
            "OPS": {
                "user": os.environ.get("OPS_SSH_USER", "root"),
                "password": os.environ.get("OPS_SSH_PASSWORD", ""),
            },
            "VOSS": {
                "user": os.environ.get("VOSS_SSH_USER", "root"),
                "password": "",                      # per-box, resolved from json
                "per_host": _parse_json_env("VOSS_CREDS_JSON"),
            },
        }
        # optional dedicated key (preferred long-term)
        key = os.environ.get("PULSE_SSH_KEY", "")
        object.__setattr__(s, "creds", creds)
        object.__setattr__(s, "ssh_key", key)
        return s


def _parse_json_env(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


settings = Settings.load()
