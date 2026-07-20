"""Pulse — agentless self-healing control plane for a multi-vendor VoIP fleet."""

__version__ = "0.1.0"      # Phase 1 — Observer (read-only)
# Bumped on every deploy so /health tells you exactly which build is live —
# the only reliable way to catch a stale/cached Coolify image.
__build__ = "phase5-alerting"
