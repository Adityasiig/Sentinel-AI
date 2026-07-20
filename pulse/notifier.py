"""Alerting — Phase 5. Proactive notifications on incident open/resolve.

The connective tissue between detection (Phase 2) and auto-heal (Phase 3): when
an incident crosses the alert threshold Pulse pages a human once, and pings a
recovery when it clears. Read-only — this module sends *messages*, it never
touches a production box or the governor.

Design rules baked in here:

1. **Opt-in, degrade gracefully.** Channels arm only when their env vars are set
   (`PULSE_ALERT_WEBHOOK`, `PULSE_TELEGRAM_TOKEN`+`PULSE_TELEGRAM_CHAT`). With
   none set, `available()` is False and the whole feature is inert — no errors,
   the dashboard tab shows a "not connected" state.
2. **Alert once per transition.** Dedup is enforced in the DB (`notifications`
   table, UNIQUE(incident_id, event)). A failure that persists across every 60s
   sweep pages exactly once, not 1,440 times a day.
3. **Severity gate.** Only incidents at/above `PULSE_ALERT_MIN_SEVERITY` page.
   `needs-human` (a crit probe with no vetted playbook) is treated as critical.

Transport is stdlib `urllib` (no requests/httpx in the image). Sends are
blocking, so the prober calls `process()` in a thread executor.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import db
from .config import settings
from .playbooks import load_playbooks

# severity ordering for the min-severity gate; needs-human maps to critical
_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}


def _severity_for_kind(kind: str) -> str:
    """Resolve an incident kind to a severity string.

    Known failures carry their playbook's severity. `unknown:<probe>`
    (needs-human) has no vetted fix and is always treated as critical.
    """
    if kind.startswith("unknown:"):
        return "critical"
    for pb in load_playbooks():
        if pb.id == kind:
            return pb.severity
    return "warning"


def _passes_gate(kind: str) -> bool:
    floor = _SEV_RANK.get(settings.alert_min_severity, 2)
    return _SEV_RANK.get(_severity_for_kind(kind), 1) >= floor


# ── channels ─────────────────────────────────────────────────────────────
def channels_configured() -> list[str]:
    """Human-readable list of armed channels (for /api/status + the UI)."""
    ch: list[str] = []
    if settings.alert_webhook:
        url = settings.alert_webhook.lower()
        if "hooks.slack.com" in url:
            ch.append("slack")
        elif "discord.com" in url or "discordapp.com" in url:
            ch.append("discord")
        else:
            ch.append("webhook")
    if settings.telegram_token and settings.telegram_chat:
        ch.append("telegram")
    return ch


def available() -> bool:
    return bool(channels_configured())


def _post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _send_webhook(text: str) -> None:
    """Post to a generic/Slack/Discord incoming webhook.

    Slack and Discord both accept a plain `{"text": ...}` / `{"content": ...}`
    body; we send both keys so one payload fits every common webhook target.
    """
    url = settings.alert_webhook
    low = url.lower()
    if "discord.com" in low or "discordapp.com" in low:
        _post_json(url, {"content": text})
    else:  # slack + generic
        _post_json(url, {"text": text})


def _send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    _post_json(url, {"chat_id": settings.telegram_chat, "text": text,
                     "parse_mode": "Markdown", "disable_web_page_preview": True})


def _broadcast(text: str) -> list[str]:
    """Send `text` to every armed channel. Returns the channels that succeeded."""
    sent: list[str] = []
    if settings.alert_webhook:
        try:
            _send_webhook(text)
            sent.append("webhook")
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
    if settings.telegram_token and settings.telegram_chat:
        try:
            _send_telegram(text)
            sent.append("telegram")
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
    return sent


# ── message bodies ───────────────────────────────────────────────────────
def _fmt_open(inc: dict) -> str:
    sev = _severity_for_kind(inc["kind"]).upper()
    icon = "🔴" if sev == "CRITICAL" else "🟠" if sev == "WARNING" else "🔵"
    human = " (needs human — no vetted fix)" if inc["kind"].startswith("unknown:") else ""
    return (f"{icon} *Pulse* — incident opened\n"
            f"*{inc['host_id']}* ({inc['ip']}, {inc['role']})\n"
            f"`{inc['kind']}` · {sev}{human}")


def _fmt_resolve(inc: dict) -> str:
    dur = ""
    if inc.get("closed_ts") and inc.get("opened_ts"):
        mins = max(1, int((inc["closed_ts"] - inc["opened_ts"]) / 60))
        dur = f" · down {mins}m"
    return (f"✅ *Pulse* — recovered\n"
            f"*{inc['host_id']}* ({inc['ip']}, {inc['role']})\n"
            f"`{inc['kind']}`{dur}")


# ── the sweep hook ───────────────────────────────────────────────────────
def process() -> dict:
    """Diff live incidents against what's been alerted; page the new transitions.

    Called once per prober sweep. Idempotent and cheap when nothing changed:
    the two DB queries return empty and we return zeros. Recording the
    notification *before* checking the send result would drop alerts on a
    transient channel outage, so we only record on a successful broadcast —
    a failed page retries next sweep.
    """
    if not available():
        return {"opened": 0, "resolved": 0, "channels": []}

    opened = resolved = 0
    for inc in db.pending_open_alerts():
        if not _passes_gate(inc["kind"]):
            # below threshold: mark as handled so we don't re-evaluate forever
            db.record_notification(inc["id"], "opened")
            continue
        if _broadcast(_fmt_open(inc)):
            db.record_notification(inc["id"], "opened")
            opened += 1

    for inc in db.pending_resolved_alerts():
        if _broadcast(_fmt_resolve(inc)):
            db.record_notification(inc["id"], "resolved")
            resolved += 1

    if opened or resolved:
        db.audit("notifier", "alert", detail={"opened": opened, "resolved": resolved})
    return {"opened": opened, "resolved": resolved, "channels": channels_configured()}


def send_test() -> dict:
    """Fire a synthetic alert to every armed channel (UI 'Send Test' button)."""
    if not available():
        return {"ok": False, "error": "no channels configured", "channels": []}
    text = (f"🧪 *Pulse* — test alert\n"
            f"Alerting is wired up. {len(channels_configured())} channel(s) armed.\n"
            f"_ts {int(time.time())}_")
    sent = _broadcast(text)
    return {"ok": bool(sent), "sent": sent, "channels": channels_configured()}
