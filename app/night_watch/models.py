"""Typed domain models shared by the Night Watch agents.

Everything an agent emits is a pydantic model so the graph state, the audit
chain, the eval harness and the API all speak the same shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

FaultClass = Literal[
    "api_latency",
    "conveyor_jam",
    "db_connection_exhaustion",
    "ingest_backlog",
    "dock_fault",
    "unknown",
]

ActionName = Literal[
    "restart_service",
    "clear_jam",
    "drain_dock",
    "roll_worker_pool",
    "throttle_ingest",
    "refuse",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Alert(BaseModel):
    """One firing alert from the Grafana data plane."""

    name: str
    service: str
    severity: Literal["critical", "warning", "info"] = "critical"
    value: float = 0.0
    threshold: float = 0.0
    labels: dict[str, str] = Field(default_factory=dict)
    fingerprint: str = ""
    fired_at: str = Field(default_factory=lambda: utcnow().isoformat())


class MetricSeries(BaseModel):
    """A single PromQL series excerpt used as evidence."""

    query: str
    labels: dict[str, str] = Field(default_factory=dict)
    values: list[float] = Field(default_factory=list)
    unit: str = ""


class LogExcerpt(BaseModel):
    """Loki log lines used as evidence."""

    query: str
    lines: list[str] = Field(default_factory=list)
    service: str = ""


class EvidenceBundle(BaseModel):
    """Everything the Diagnostician is allowed to reason over."""

    alerts: list[Alert] = Field(default_factory=list)
    metrics: list[MetricSeries] = Field(default_factory=list)
    logs: list[LogExcerpt] = Field(default_factory=list)
    memory_hits: list[dict] = Field(default_factory=list)
    gathered_at: str = Field(default_factory=lambda: utcnow().isoformat())


class Diagnosis(BaseModel):
    """Structured output of the Diagnostician agent."""

    root_cause_class: FaultClass = "unknown"
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    blast_radius: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    recommended_action: ActionName = "refuse"


class PlaybookAction(BaseModel):
    """Structured output of the Remediator agent."""

    action: ActionName = "refuse"
    target: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    rationale: str = ""
    risk: Literal["low", "medium", "high"] = "medium"


class GateDecision(BaseModel):
    """Output of the deterministic policy gate (Agent Identity enforcement)."""

    decision: Literal["execute", "refuse", "hold_for_approval"]
    reasons: list[str] = Field(default_factory=list)
    checked_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ExecutionResult(BaseModel):
    """Result of executing a playbook action through the action plane."""

    action_id: str
    action: ActionName
    target: str
    status: Literal["executed", "skipped_duplicate", "failed"] = "failed"
    detail: str = ""
    executed_at: str = Field(default_factory=lambda: utcnow().isoformat())


class Verification(BaseModel):
    """Structured output of the Verifier agent."""

    verdict: Literal["verified", "failed", "uncertain"] = "uncertain"
    summary: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class IncidentRecord(BaseModel):
    """The Scribe's postmortem record — what lands in the audit chain."""

    run_id: str
    started_at: str = ""
    ended_at: str = Field(default_factory=lambda: utcnow().isoformat())
    alerts: list[Alert] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    proposal: PlaybookAction | None = None
    gate: GateDecision | None = None
    execution: ExecutionResult | None = None
    verification: Verification | None = None
    outcome: Literal["remediated", "refused", "escalated", "no_action", "blocked"] = "no_action"
    duration_s: float = 0.0
