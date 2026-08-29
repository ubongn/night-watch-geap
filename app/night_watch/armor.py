"""Inbound screening — the Model Armor lane.

Alert webhooks are untrusted input. Before anything reaches an agent, the
gateway screens the payload. The primary implementation calls Vertex AI
Model Armor when configured; the local heuristic screen always runs first
(cheap, deterministic, testable) so the security posture holds even when
the cloud filter is not reachable.

The eval harness includes a prompt-injection trap: an alert webhook whose
"annotations" field carries instructions to the agent. The screen must
refuse it before any LLM sees the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all|any|previous|prior) (instructions|prompts)",
        r"disregard .{0,30}(instructions|rules)",
        r"you are now",
        r"system prompt",
        r"reveal .{0,30}(prompt|instructions|key|token)",
        r"execute .{0,30}regardless",
        r"<\|.*\|>",  # fake system/channel markers
        r"sudo ",  # shell-ish commands riding in alert text
        r"curl -",
        r"rm -rf",
    )
)

MAX_LABEL_LEN = 512


@dataclass
class ScreenResult:
    verdict: Literal["allow", "block"]
    reasons: list[str] = field(default_factory=list)


def _scan_text(text: str) -> list[str]:
    found = []
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            found.append(f"pattern {pat.pattern!r} matched {m.group(0)!r}")
    if len(text) > MAX_LABEL_LEN * 16:
        found.append("payload suspiciously large")
    return found


def screen_webhook(payload: dict) -> ScreenResult:
    """Local heuristic screen of an inbound webhook payload.

    Scans every string field at the top level AND inside each alert object —
    real webhook payloads carry annotations/labels per alert, so the screen
    must cover both depths.
    """
    reasons: list[str] = []
    targets: list[dict] = [payload]
    alerts = payload.get("alerts")
    if isinstance(alerts, list):
        targets.extend(a for a in alerts if isinstance(a, dict))
    for target in targets:
        for key in ("annotations", "message", "text", "labels", "description", "title", "valueString", "value_string"):
            val = target.get(key)
            if isinstance(val, str):
                reasons.extend(_scan_text(val))
            elif isinstance(val, dict):
                for v in val.values():
                    if isinstance(v, str):
                        reasons.extend(_scan_text(v))
    if reasons:
        return ScreenResult(verdict="block", reasons=reasons)
    return ScreenResult(verdict="allow", reasons=[])


async def screen_with_model_armor(payload: dict, template: str, project: str, location: str) -> ScreenResult:
    """Screen via Vertex AI Model Armor (optional cloud filter).

    Uses the Model Armor REST endpoint with templates configured out-of-band.
    Falls back to the local screen on any transport/config problem — the
    security posture never depends on the network being up.
    """
    local = screen_webhook(payload)
    if local.verdict == "block":
        return local
    if not (template and project):
        return local
    try:
        import httpx

        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore

        creds_path = __import__("os").environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not creds_path:
            return local
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(Request())
        url = (
            f"https://modelarmor.{location}.rep.googleapis.com/v1/projects/{project}"
            f"/locations/{location}/templates/{template}:screen"
        )
        body = {"payload": {"text": __import__("json").dumps(payload)[:8192]}}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, json=body, headers={"authorization": f"Bearer {creds.token}"}
            )
            resp.raise_for_status()
            data = resp.json()
        filters = data.get("serverResponse", data)
        verdict = str(filters).lower()
        if "blocked" in verdict or '"match": "true"' in verdict:
            return ScreenResult(verdict="block", reasons=["model armor matched"])
        return ScreenResult(verdict="allow")
    except Exception:  # noqa: BLE001 — never fail open OR closed on infra
        return local
