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
    ssh_concurrency: int = _int("PULSE_SSH_CONCURRENCY", 20)    # max parallel SSH sessions; 20 sweeps the 82-box fleet in ~12s (measured) vs ~22s at 10
    ssh_timeout: int = _int("PULSE_SSH_TIMEOUT", 12)            # per-host connect+run budget
    # Read-only until explicitly flipped. Phases 1–2 keep this False, hard.
    remediation_enabled: bool = os.environ.get("PULSE_REMEDIATION_ENABLED", "").lower() in ("1", "true", "yes")

    # ── remediation governor (Phase 3) ───────────────────────────────────
    remediation_timeout: int = _int("PULSE_REMEDIATION_TIMEOUT", 120)  # per-step run budget (restarts are slow)
    blast_radius: int = _int("PULSE_BLAST_RADIUS", 3)          # max executions allowed per window
    blast_window: int = _int("PULSE_BLAST_WINDOW", 600)        # window length in seconds (default 10 min)
    # Autonomous auto-heal (no human click). OFF by default: even auto_approve
    # playbooks require a manual approval until this is explicitly enabled.
    autoheal_enabled: bool = os.environ.get("PULSE_AUTOHEAL", "").lower() in ("1", "true", "yes")

    # ── AI copilot (Phase 4) ─────────────────────────────────────────────
    # Local Ollama / OpenAI-compatible endpoint ONLY. Unset => feature off and
    # the tab degrades gracefully. Fleet data never leaves the box: this is the
    # base URL of a model running on our own infra, nothing external.
    llm_url: str = os.environ.get("PULSE_LLM_URL", "").strip()
    llm_model: str = os.environ.get("PULSE_LLM_MODEL", "llama3.1:8b").strip()
    llm_timeout: int = _int("PULSE_LLM_TIMEOUT", 180)  # CPU 7B/8B inference is slow
    # Optional bearer for a token-gated model endpoint (our auth proxy in front
    # of Ollama). Unset => no Authorization header sent.
    llm_token: str = os.environ.get("PULSE_LLM_TOKEN", "").strip()

    # ── alerting (Phase 5) ───────────────────────────────────────────────
    # Proactive notifications when incidents open/resolve. All channels are
    # opt-in via env; unset => feature off and the tab degrades gracefully.
    # Generic/Slack/Discord incoming webhook (auto-detected by URL shape).
    alert_webhook: str = os.environ.get("PULSE_ALERT_WEBHOOK", "").strip()
    # Telegram bot: both token and chat id required to arm the channel.
    telegram_token: str = os.environ.get("PULSE_TELEGRAM_TOKEN", "").strip()
    telegram_chat: str = os.environ.get("PULSE_TELEGRAM_CHAT", "").strip()
    # Only page for incidents at/above this severity. critical | warning | info.
    # 'critical' also covers needs-human (a crit probe with no vetted playbook).
    alert_min_severity: str = os.environ.get("PULSE_ALERT_MIN_SEVERITY", "critical").strip().lower()

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
