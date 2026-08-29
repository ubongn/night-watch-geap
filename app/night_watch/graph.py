"""The Night Watch graph — five specialized agents on one ADK 2 workflow.

Deterministic business rules (routing, gates, execution, audit) are plain
function nodes; probabilistic reasoning (diagnosis, proposal synthesis,
verification judgment) lives in LlmAgent nodes behind output schemas. That
split is deliberate: the webinar's "deterministic rules + probabilistic
reasoning" pattern, rendered in code.

    START -> detector -> evidence -> diagnostician -> capture_diagnosis
          -> remediator -> capture_proposal -> policy_gate
          -> {execute: executor -> verifier -> capture_verification
                     -> {verified: scribe | failed: scribe | uncertain: scribe},
              refuse: scribe, hold_for_approval: scribe}

Every terminal path funnels through the Scribe, which writes the
tamper-evident audit record and the Grafana annotation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.workflow import START, Workflow, node
from pydantic import BaseModel, ValidationError

from . import models
from .actions import ActionExecutor, validate as validate_action
from .audit import AuditChain
from .grafana import GrafanaClient
from .memory import MemoryBank
from .model_provider import build_model
from .models import (
    Diagnosis,
    EvidenceBundle,
    GateDecision,
    IncidentRecord,
    PlaybookAction,
    Verification,
)

# ---------------------------------------------------------------------------
# Schemas for the LLM agents (structured output keeps the fleet honest)
# ---------------------------------------------------------------------------


class DiagnosisSchema(BaseModel):
    root_cause_class: str
    summary: str
    confidence: float
    blast_radius: str
    evidence_refs: list[str]
    recommended_action: str


class ProposalSchema(BaseModel):
    action: str
    target: str
    params: dict[str, str]
    rationale: str
    risk: str


class VerificationSchema(BaseModel):
    verdict: str
    summary: str
    evidence_refs: list[str]
    confidence: float


DIAGNOSTICIAN_INSTRUCTION = """You are the Diagnostician of Night Watch, an autonomous SRE fleet
guarding the night shift of Meridian Freight (a 40-person parcel logistics company).

You receive an evidence bundle: firing alerts, Prometheus metric excerpts and Loki log
lines, plus any incident-memory hits. Diagnose the root cause. Rules:
- Reason ONLY over the provided evidence. If the evidence does not support a fault, say so.
- root_cause_class must be one of: api_latency, conveyor_jam, db_connection_exhaustion,
  ingest_backlog, dock_fault, unknown.
- recommended_action must be one of: restart_service, clear_jam, drain_dock,
  roll_worker_pool, throttle_ingest, refuse.
- If memory_hits show a previously remediated incident with the same signature,
  weight that recommendation and say so in the summary.
- Never recommend an action when the evidence shows normal/benign behavior (e.g. a
  planned traffic spike with healthy error rates): use "refuse" and explain.
Respond with JSON matching the required schema only."""

REMEDIATOR_INSTRUCTION = """You are the Remediator of Night Watch. Given the Diagnostician's finding,
select the single best playbook action and its parameters. You may ONLY choose from the
registry: restart_service(service), clear_jam(dock), roll_worker_pool(service),
throttle_ingest(topic), drain_dock(dock), or refuse (do nothing). Services: dispatch-api,
sorter-conveyor, wms-postgres, gps-ingest, charge-docks. Docks look like dock-1..dock-6.
Topics: gps.events, scan.events, route.events. If the diagnosis is 'unknown' or
confidence < 0.6, choose refuse. Respond with JSON matching the required schema only."""

VERIFIER_INSTRUCTION = """You are the Verifier of Night Watch. A remediation action was executed;
you receive the post-action evidence (same metric/log families as before). Decide:
'verified' if the fault signature is gone and the service recovered; 'failed' if the
condition persists; 'uncertain' if evidence is insufficient. Be strict — an action is
only verified when the numbers say so. Respond with JSON matching the required schema only."""


# ---------------------------------------------------------------------------
# Function nodes (deterministic business rules)
# ---------------------------------------------------------------------------


@node
async def detector(ctx: Context, alert_webhook: dict) -> None:
    """Detect: normalize the (already Armor-screened) alert webhook."""
    alerts = [models.Alert(**a) for a in (alert_webhook.get("alerts") or [])]
    ctx.state["alerts"] = [a.model_dump() for a in alerts]
    ctx.state["detected_at"] = models.utcnow().isoformat()
    ctx.state["run_started_monotonic"] = models.utcnow().timestamp()
    ctx.route = "fired" if alerts else "quiet"


@node
async def evidence(ctx: Context) -> None:
    """Gather the evidence pack: metrics, logs, incident-memory hits."""
    grafana: GrafanaClient = ctx.state["_deps"]["grafana"]
    memory: MemoryBank = ctx.state["_deps"]["memory"]
    alerts = [models.Alert(**a) for a in ctx.state.get("alerts", [])]
    services = sorted({a.service for a in alerts})

    metrics: list[dict] = []
    logs: list[dict] = []
    for service in services:
        pack = await grafana.evidence_for(service)
        metrics.extend(m.model_dump() for m in pack["metrics"])
        logs.append(pack["logs"].model_dump())

    mem_hits: list[dict] = []
    for a in alerts:
        for hit in memory.query(a.service, "unknown", limit=2):
            mem_hits.append(hit)
    # also try the likely class from the alert name
    for a in alerts:
        guess = next(
            (fc for fc in ("api_latency", "conveyor_jam", "db_connection_exhaustion",
                           "ingest_backlog", "dock_fault") if fc in a.name.replace("-", "_")),
            None,
        )
        if guess:
            mem_hits.extend(memory.query(a.service, guess, limit=2))

    bundle = EvidenceBundle(
        alerts=[a.model_dump() for a in alerts],
        metrics=metrics[:12],
        logs=logs[:6],
        memory_hits=mem_hits[:4],
    )
    ctx.state["evidence"] = bundle.model_dump()
    ctx.state["proposal_clock_start"] = models.utcnow().isoformat()
    # The bundle is this node's OUTPUT: it becomes the Diagnostician's user turn.
    return _llm_user_content(DIAGNOSTICIAN_INSTRUCTION, bundle.model_dump())


def _llm_user_content(instruction: str, payload: dict) -> str:
    return "DATA FOR ANALYSIS:\n" + json.dumps(payload, indent=1, default=str)[:24000]


diagnostician = LlmAgent(
    name="diagnostician",
    model=build_model(),
    instruction=DIAGNOSTICIAN_INSTRUCTION,
    output_key="diagnosis_raw",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

remediator = LlmAgent(
    name="remediator",
    model=build_model(),
    instruction=REMEDIATOR_INSTRUCTION,
    output_key="proposal_raw",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

verifier_agent = LlmAgent(
    name="verifier",
    model=build_model(),
    instruction=VERIFIER_INSTRUCTION,
    output_key="verification_raw",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)


@node
async def capture_diagnosis(ctx: Context, node_input: str) -> None:
    """Validate + persist the Diagnostician's structured output."""
    audit: AuditChain = ctx.state["_deps"]["audit"]
    run_id = ctx.state.get("run_id", "unknown")
    try:
        parsed = json.loads(node_input)
        diagnosis = Diagnosis(**DiagnosisSchema(**parsed).model_dump())
        ctx.state["diagnosis"] = diagnosis.model_dump()
        audit.append("diagnosis", run_id, diagnosis.model_dump())
        payload = {
            "diagnosis": diagnosis.model_dump(),
            "alerts": ctx.state.get("alerts", []),
            "memory_hits": (ctx.state.get("evidence") or {}).get("memory_hits", []),
        }
        return _llm_user_content(REMEDIATOR_INSTRUCTION, payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        ctx.state["diagnosis"] = Diagnosis(
            root_cause_class="unknown", summary=f"unparseable diagnosis: {exc}", confidence=0.0
        ).model_dump()
        audit.append("diagnosis_parse_failed", run_id, {"error": str(exc)[:400]})
    ctx.route = "propose"


@node
async def capture_proposal(ctx: Context, node_input: str) -> None:
    """Validate + persist the Remediator's proposed action."""
    audit: AuditChain = ctx.state["_deps"]["audit"]
    run_id = ctx.state.get("run_id", "unknown")
    try:
        parsed = json.loads(node_input)
        proposal = PlaybookAction(**ProposalSchema(**parsed).model_dump())
        ctx.state["proposal"] = proposal.model_dump()
        audit.append("proposal", run_id, proposal.model_dump())
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        ctx.state["proposal"] = PlaybookAction(action="refuse", rationale=f"unparseable proposal: {exc}").model_dump()
        audit.append("proposal_parse_failed", run_id, {"error": str(exc)[:400]})
    ctx.route = "gate"


@node
async def policy_gate(ctx: Context) -> None:
    """Deterministic policy gate: Agent Identity scopes + risk rules + approval.

    This is where 'the agent proposed it' becomes 'the fleet is allowed to
    do it'. No LLM involved — by design.
    """
    audit: AuditChain = ctx.state["_deps"]["audit"]
    run_id = ctx.state.get("run_id", "unknown")
    proposal = PlaybookAction(**ctx.state.get("proposal", {}))
    diagnosis = Diagnosis(**ctx.state.get("diagnosis", Diagnosis().model_dump()))
    reasons: list[str] = []

    from .identity import get_identity

    remediator_id = get_identity("remediator")
    if not remediator_id or not remediator_id.has("actions:propose"):
        reasons.append("remediator identity lacks actions:propose")

    problems = validate_action(proposal)
    reasons.extend(problems)

    if proposal.action != "refuse":
        if diagnosis.root_cause_class == "unknown":
            reasons.append("refusing: root cause unknown")
        if diagnosis.confidence < 0.6:
            reasons.append("refusing: diagnosis confidence below 0.6")

    approval_policy = ctx.state.get("approval_policy", "auto_approve")
    if not reasons and proposal.risk == "high" and approval_policy != "auto_approve":
        decision = "hold_for_approval"
        reasons.append("high-risk action requires human approval")
    elif reasons and proposal.action != "refuse":
        decision = "refuse"
    else:
        decision = "refuse" if proposal.action == "refuse" else "execute"

    gate = GateDecision(decision=decision, reasons=reasons)  # type: ignore[arg-type]
    ctx.state["gate"] = gate.model_dump()
    audit.append("gate", run_id, gate.model_dump())
    ctx.route = decision


@node
async def executor(ctx: Context) -> None:
    """Execute the gated action — idempotently — on the action plane."""
    audit: AuditChain = ctx.state["_deps"]["audit"]
    executor_: ActionExecutor = ctx.state["_deps"]["executor"]
    run_id = ctx.state.get("run_id", "unknown")
    proposal = PlaybookAction(**ctx.state.get("proposal", {}))

    if proposal.action == "refuse":
        ctx.state["execution"] = None
        ctx.route = "verify"
        return

    result = await executor_.execute(run_id, proposal)
    ctx.state["execution"] = result.model_dump()
    audit.append("execution", run_id, result.model_dump())
    ctx.route = "verify"


@node
async def post_action_evidence(ctx: Context) -> None:
    """Cooldown, then fetch fresh evidence for the Verifier."""
    import asyncio

    grafana: GrafanaClient = ctx.state["_deps"]["grafana"]
    alerts = [models.Alert(**a) for a in ctx.state.get("alerts", [])]
    await asyncio.sleep(float(ctx.state.get("verify_cooldown_s", 2)))

    metrics: list[dict] = []
    logs: list[dict] = []
    for service in sorted({a.service for a in alerts}):
        pack = await grafana.evidence_for(service, window_minutes=3)
        metrics.extend(m.model_dump() for m in pack["metrics"])
        logs.append(pack["logs"].model_dump())
    ctx.state["post_evidence"] = {
        "metrics": metrics[:12],
        "logs": logs[:6],
        "prior_diagnosis": ctx.state.get("diagnosis", {}),
        "action_taken": ctx.state.get("execution"),
    }
    return _llm_user_content(VERIFIER_INSTRUCTION, ctx.state["post_evidence"])


@node
async def capture_verification(ctx: Context, node_input: str) -> None:
    audit: AuditChain = ctx.state["_deps"]["audit"]
    run_id = ctx.state.get("run_id", "unknown")
    try:
        parsed = json.loads(node_input)
        verification = Verification(**VerificationSchema(**parsed).model_dump())
        ctx.state["verification"] = verification.model_dump()
        audit.append("verification", run_id, verification.model_dump())
        ctx.route = "verified" if verification.verdict == "verified" else "escalate"
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        ctx.state["verification"] = Verification(verdict="uncertain", summary=f"unparseable: {exc}").model_dump()
        audit.append("verification_parse_failed", run_id, {"error": str(exc)[:400]})
        ctx.route = "escalate"


@node
async def scribe(ctx: Context) -> dict:
    """Write the incident record: audit chain, memory, Grafana annotation."""
    audit: AuditChain = ctx.state["_deps"]["audit"]
    memory: MemoryBank = ctx.state["_deps"]["memory"]
    grafana: GrafanaClient = ctx.state["_deps"]["grafana"]
    run_id = ctx.state.get("run_id", "unknown")

    diagnosis = ctx.state.get("diagnosis")
    proposal = ctx.state.get("proposal")
    execution = ctx.state.get("execution")
    verification = ctx.state.get("verification")

    if ctx.state.get("alerts"):
        outcome = "no_action"
        if execution and execution.get("status") == "executed":
            outcome = "remediated" if verification and verification.get("verdict") == "verified" else "escalated"
        elif proposal and proposal.get("action") == "refuse":
            outcome = "refused"
        elif ctx.state.get("gate", {}).get("decision") == "refuse":
            outcome = "refused"
        elif ctx.state.get("gate", {}).get("decision") == "hold_for_approval":
            outcome = "escalated"
    else:
        outcome = "no_action"

    started = ctx.state.get("run_started_monotonic") or models.utcnow().timestamp()
    record = IncidentRecord(
        run_id=run_id,
        started_at=ctx.state.get("detected_at", ""),
        alerts=[models.Alert(**a) for a in ctx.state.get("alerts", [])],
        diagnosis=Diagnosis(**diagnosis) if diagnosis else None,
        proposal=PlaybookAction(**proposal) if proposal else None,
        gate=GateDecision(**ctx.state["gate"]) if ctx.state.get("gate") else None,
        execution=models.ExecutionResult(**execution) if execution else None,
        verification=Verification(**verification) if verification else None,
        outcome=outcome,  # type: ignore[arg-type]
        duration_s=round(models.utcnow().timestamp() - float(started), 2),
    )
    ctx.state["incident_record"] = record.model_dump()
    audit.append("incident_record", run_id, record.model_dump())
    if outcome in ("remediated", "refused"):
        memory.remember(record)

    # Best-effort Grafana annotation (never fails the run).
    try:
        await grafana.annotate(
            f"Night Watch [{outcome}] {record.diagnosis.root_cause_class if record.diagnosis else 'n/a'}"
            f" — {run_id}",
            tags=["night-watch", outcome],
        )
    except Exception:  # noqa: BLE001
        pass
    return {"run_id": run_id, "outcome": outcome}


def build_workflow(provider: str | None = None):
    """Assemble the Night Watch workflow graph."""
    global diagnostician, remediator, verifier_agent
    model = build_model(provider)
    diagnostician = diagnostician.model_copy(update={"model": model})
    remediator = remediator.model_copy(update={"model": model})
    verifier_agent = verifier_agent.model_copy(update={"model": model})

    return Workflow(
        name="night_watch",
        description="Five-agent SRE fleet for the Meridian Freight night shift.",
        edges=[
            (START, detector),
            (detector, {"fired": evidence, "quiet": scribe}),
            (evidence, diagnostician),
            (diagnostician, capture_diagnosis),
            (capture_diagnosis, remediator),
            (remediator, capture_proposal),
            (capture_proposal, policy_gate),
            (policy_gate, {"execute": executor, "refuse": scribe, "hold_for_approval": scribe}),
            (executor, post_action_evidence),
            (post_action_evidence, verifier_agent),
            (verifier_agent, capture_verification),
            (capture_verification, {"verified": scribe, "escalate": scribe}),
        ],
    )
