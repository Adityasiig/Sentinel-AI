"""Local LLM client — Phase 4 AI Copilot.

Talks to a *self-hosted* Ollama / OpenAI-compatible endpoint and nothing else.
Two hard rules baked in here, not left to the caller:

1. **Local-only.** The endpoint is whatever `PULSE_LLM_URL` points at (our own
   Ollama on this infra). If it's unset the whole feature is off — `available()`
   returns False and every call raises `LLMUnavailable`. Fleet data never gets
   POSTed to a third party because there's no code path that can reach one.
2. **Advisory-only.** This module returns *text*. It cannot open incidents, run
   playbooks, or reach the governor. AI output is copy-for-a-human, full stop.

Transport is stdlib `urllib` (no httpx/aiohttp in the image). Requests are
blocking, so async callers run `ask()`/`analyze_incident()` in a thread executor.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import settings


class LLMUnavailable(RuntimeError):
    """Raised when no local model endpoint is configured or it can't be reached."""


SYSTEM_ASK = (
    "You are Pulse Copilot, a terse senior SRE assistant embedded in a VoIP fleet "
    "control plane. You are given read-only health data for a fleet of FreeSWITCH "
    "(IVG), OpenSIPS (OPS) and VOS3000 (VOSS) boxes. Answer operational questions "
    "using ONLY the data provided. Be concise and concrete. If the data doesn't "
    "contain the answer, say so plainly instead of guessing. Never invent hosts, "
    "metrics, or commands. You cannot run anything — you only advise a human."
)

SYSTEM_ANALYZE = (
    "You are Pulse Copilot, a senior SRE triaging a single production incident that "
    "no vetted playbook covers ('needs-human'). You are given the host, its role, the "
    "failing probes and recent probe history. Produce a short triage: (1) most likely "
    "root cause, (2) the exact read-only commands an operator should run to confirm, "
    "(3) the likely fix — clearly flagged as a SUGGESTION for a human to review. "
    "Do not claim to have run anything. Keep it under ~200 words."
)


def available() -> bool:
    """True iff a local model endpoint is configured."""
    return bool(settings.llm_url)


def _generate(system: str, prompt: str) -> str:
    if not available():
        raise LLMUnavailable("no local model configured (PULSE_LLM_URL unset)")

    url = settings.llm_url.rstrip("/") + "/api/generate"
    payload = json.dumps({
        "model": settings.llm_model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if settings.llm_token:
        headers["Authorization"] = "Bearer " + settings.llm_token
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise LLMUnavailable(f"local model unreachable at {settings.llm_url}: {e}") from e
    except TimeoutError as e:  # slow CPU inference blew the budget
        raise LLMUnavailable(f"local model timed out after {settings.llm_timeout}s") from e

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"local model returned non-JSON: {body[:200]}") from e

    text = (data.get("response") or "").strip()
    if not text:
        raise LLMUnavailable("local model returned an empty response")
    return text


def ask(question: str, context: str) -> str:
    """Free-form Q&A grounded on the supplied non-secret fleet context."""
    prompt = f"# Fleet data\n{context}\n\n# Question\n{question.strip()}\n\n# Answer"
    return _generate(SYSTEM_ASK, prompt)


def analyze_incident(context: str) -> str:
    """Triage one needs-human incident from its non-secret context block."""
    prompt = f"# Incident\n{context}\n\n# Triage"
    return _generate(SYSTEM_ANALYZE, prompt)
