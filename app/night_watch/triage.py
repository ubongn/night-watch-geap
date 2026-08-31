"""Gemma triage tier — cheap first-pass classification at the gateway.

Tiered agent intelligence: while the fleet's reasoning tier runs on the full
Gemini model, every inbound (Armor-cleared) alert bundle first gets a fast,
cheap read from a small Gemma model on the same AI Studio key: severity band,
affected service, likely fault class, and a duplicate / known-incident check
against incident memory. That classification rides along as advisory context
for the Diagnostician and is recorded on the audit chain.

Fail-open is a hard invariant of this module: triage runs concurrently with
the run's evidence gathering, joins with a wall-clock budget, and ANY failure
(model error, HTTP error, unparseable turn, timeout) degrades to "no triage"
without ever raising into the ingest path. An incident must never wait on the
cheap tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import SETTINGS
from .memory import MemoryBank

log = logging.getLogger("night_watch.triage")

REQUIRED_KEYS = ("severity_band", "service", "fault_class")

PROMPT_TEMPLATE = """You are the triage tier of Night Watch, an autonomous SRE gateway for Meridian Freight. An alert bundle has passed input screening. Give a fast first-pass classification.

Reply with ONLY a JSON object (no markdown, no prose) with exactly these keys:
{{"severity_band": "low|medium|high|critical", "service": "<most-affected service>", "fault_class": "<api_latency|conveyor_jam|db_connection_exhaustion|ingest_backlog|dock_fault|unknown>", "duplicate": <true if this looks like a repeat of a known incident below, else false>, "known_incident": "<matching incident signature from memory, or null>", "confidence": <0.0-1.0>, "rationale": "<one short sentence>"}}

Alert bundle:
{alerts}

Recent incident memory (for the duplicate check):
{memory}"""


@dataclass
class TriageResult:
    """Outcome of one triage attempt (or degradation)."""

    model: str = ""
    classification: dict | None = None
    degraded: bool = False
    reason: str = ""
    latency_s: float = 0.0

    def audit_entry(self) -> dict:
        return {
            "agent": "triage",
            "model": self.model or "none",
            "status": "degraded" if self.degraded else "ok",
            "reason": self.reason,
            "latency_s": round(self.latency_s, 3),
            "classification": self.classification,
        }


def _parse_json(text: str) -> dict:
    """Fence/preamble-tolerant JSON parse (same policy as graph.extract_json,
    kept local so this module has no graph-layer imports)."""
    stripped = (text or "").strip()
    if not stripped:
        raise json.JSONDecodeError("empty gemma turn", text or "", 0)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    if "```" in stripped:  # ```json ... ```
        for chunk in stripped.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
    start = stripped.find("{")  # first balanced object
    if start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise json.JSONDecodeError("no JSON object in gemma turn", text or "", 0)


def _validate(parsed: dict) -> dict:
    missing = [k for k in REQUIRED_KEYS if k not in parsed]
    if missing:
        raise ValueError(f"missing keys: {','.join(missing)}")
    return parsed


class GemmaTriage:
    """Calls the configured Gemma model chain on the Gemini API."""

    def __init__(self, api_key: str, models: list[str]):
        self.api_key = api_key
        self.models = list(models)
        self._aio_models: Any = None

    def _aio(self) -> Any:
        if self._aio_models is None:
            from google import genai

            client = genai.Client(api_key=self.api_key)
            self._aio_models = client.aio.models
        return self._aio_models

    async def classify(self, payload: dict, memory_hits: list[dict] | None = None) -> TriageResult:
        prompt = PROMPT_TEMPLATE.format(
            alerts=json.dumps(payload)[:4000],
            memory=json.dumps(memory_hits or [])[:2000] or "[]",
        )
        last_error = "no model attempted"
        for model in self.models:
            t0 = time.monotonic()
            try:
                resp = await self._aio().generate_content(model=model, contents=prompt)
            except Exception as exc:  # noqa: BLE001 — triage never raises
                last_error = f"{model}: {type(exc).__name__}: {str(exc)[:160]}"
                log.warning("gemma triage: %s", last_error)
                continue  # next model in the chain (renames, regional 404s, ...)
            latency_s = time.monotonic() - t0
            try:
                parsed = _validate(_parse_json((resp.text or "").strip()))
                return TriageResult(model=model, classification=parsed, latency_s=latency_s)
            except (json.JSONDecodeError, ValueError) as exc:
                # the model answered but not in JSON: degrade immediately —
                # burning the budget on retries only slows the fail-open path
                return TriageResult(
                    model=model,
                    degraded=True,
                    reason=f"unparseable turn from {model}: {str(exc)[:160]}",
                    latency_s=latency_s,
                )
        return TriageResult(
            degraded=True,
            reason=f"all triage models rejected; last: {last_error}",
        )


_TRIAGER: GemmaTriage | None = None


def _triager() -> GemmaTriage:
    global _TRIAGER
    if _TRIAGER is None:
        models = [m.strip() for m in SETTINGS.gemma_triage_models.split(",") if m.strip()]
        _TRIAGER = GemmaTriage(SETTINGS.gemini_api_key, models or ["gemma-3-27b-it"])
    return _TRIAGER


def enabled() -> bool:
    """The tier runs only when flagged on, the live provider is gemini, and a
    key is present. Tests/evals on the scripted provider never touch the API."""
    if SETTINGS.gemma_triage.strip().lower() in {"off", "0", "false", "no"}:
        return False
    if SETTINGS.ai_provider != "gemini":
        return False
    return bool(SETTINGS.gemini_api_key)


def _memory_context(payload: dict) -> list[dict]:
    """Recent incidents for the primary service, for the duplicate check."""
    try:
        alerts = payload.get("alerts") or []
        service = alerts[0].get("service", "unknown") if alerts else "unknown"
        bank = MemoryBank(SETTINGS.memory_dir / "incidents.jsonl")
        return bank.query(service, "unknown", limit=3)
    except Exception:  # noqa: BLE001 — advisory only
        return []


class TriageHandle:
    """Join point for the in-flight triage task.

    ``result()`` waits at most until the wall-clock budget (measured from
    webhook receipt) is spent, then degrades. The underlying task is shielded,
    so a timeout never cancels or raises into the caller.
    """

    def __init__(self, task: asyncio.Task, budget_s: float):
        self.task = task
        self.budget_s = budget_s
        self.t0 = time.monotonic()

    async def result(self) -> TriageResult:
        try:
            remaining = self.budget_s - (time.monotonic() - self.t0)
            if remaining > 0:
                out = await asyncio.wait_for(asyncio.shield(self.task), remaining)
            else:
                out = self.task.result() if self.task.done() else None
        except asyncio.TimeoutError:
            out = TriageResult(
                degraded=True, reason=f"timeout after {self.budget_s:.1f}s (fail-open)"
            )
        except asyncio.CancelledError:
            raise  # run was killed; propagate the kill, not a triage result
        except Exception as exc:  # noqa: BLE001 — belt and braces
            out = TriageResult(degraded=True, reason=f"{type(exc).__name__}: {exc}"[:200])
        return out or TriageResult(degraded=True, reason="no triage result produced")


async def _safe_classify(triager: GemmaTriage, payload: dict, hits: list[dict]) -> TriageResult:
    try:
        return await triager.classify(payload, hits)
    except Exception as exc:  # noqa: BLE001 — triage never raises
        return TriageResult(degraded=True, reason=f"{type(exc).__name__}: {exc}"[:200])


def start_triage(payload: dict) -> TriageHandle | None:
    """Fire the gateway triage tier for one alert bundle.

    Returns a join handle, or ``None`` when the tier is off / not applicable.
    Never raises: ingestion proceeds with zero triage on any start failure.
    """
    try:
        if not enabled():
            return None
        asyncio.get_running_loop()  # no loop (sync context)? triage is off, full stop
        hits = _memory_context(payload)
        task = asyncio.create_task(_safe_classify(_triager(), payload, hits))
        return TriageHandle(task, SETTINGS.gemma_triage_timeout_s)
    except Exception:  # noqa: BLE001
        log.exception("triage failed to start; proceeding without triage")
        return None
