"""Playbook action registry and the idempotent executor.

The Remediator can only propose actions from this registry — the enum in the
PlaybookAction model makes anything else a validation error. The executor is
deterministic code, not an LLM: it validates params against the registry,
checks the action ledger (idempotency: a resumed run never double-acts) and
dispatches to the simulator control plane.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .models import ExecutionResult, PlaybookAction
from .sim_client import SimClient

# target := which service/dock/topic an action applies to


@dataclass(frozen=True)
class ActionSpec:
    name: str
    target_hint: str  # human hint, validation is explicit below
    required_params: tuple[str, ...]
    neutralizes: tuple[str, ...]  # fault classes this action actually fixes


REGISTRY: dict[str, ActionSpec] = {
    "restart_service": ActionSpec(
        "restart_service", "service", ("service",), ("api_latency", "ingest_backlog")
    ),
    "clear_jam": ActionSpec(
        "clear_jam", "dock", ("dock",), ("conveyor_jam",)
    ),
    "drain_dock": ActionSpec(
        "drain_dock", "dock", ("dock",), ("dock_fault",)
    ),
    "roll_worker_pool": ActionSpec(
        "roll_worker_pool", "service", ("service",), ("db_connection_exhaustion",)
    ),
    "throttle_ingest": ActionSpec(
        "throttle_ingest", "topic", ("topic",), ("ingest_backlog",)
    ),
}

VALID_SERVICES = ("dispatch-api", "sorter-conveyor", "wms-postgres", "gps-ingest", "charge-docks")
VALID_TOPICS = ("gps.events", "scan.events", "route.events")


def action_id(run_id: str, action: str, target: str) -> str:
    """Stable id — the idempotency key for the action ledger."""
    raw = f"{run_id}:{action}:{target}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def validate(action: PlaybookAction) -> list[str]:
    """Deterministic validation of a proposed action. Returns problems."""
    problems: list[str] = []
    if action.action == "refuse":
        return problems  # refusals are always valid
    spec = REGISTRY.get(action.action)
    if spec is None:
        problems.append(f"unknown action {action.action!r}")
        return problems
    for p in spec.required_params:
        if not action.params.get(p):
            problems.append(f"missing required param {p!r} for {action.action!r}")
    if action.action in ("restart_service", "roll_worker_pool") and action.params.get("service") not in VALID_SERVICES:
        problems.append(f"unknown service {action.params.get('service')!r}")
    if action.action == "throttle_ingest" and action.params.get("topic") not in VALID_TOPICS:
        problems.append(f"unknown topic {action.params.get('topic')!r}")
    dock = action.params.get("dock")
    if action.action in ("clear_jam", "drain_dock"):
        import re

        m = re.fullmatch(r"dock-(\d+)", dock or "")
        if not m or not (1 <= int(m.group(1)) <= 6):
            problems.append(f"invalid dock {dock!r} (expected dock-1..dock-6)")
    return problems


class ActionLedger:
    """Append-only record of executed actions — the idempotency trap solution.

    A run that is killed mid-incident and resumed must not act twice on the
    same target. Every execution is keyed by a stable action_id derived from
    (run_id, action, target); duplicates become `skipped_duplicate`.
    """

    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                self._seen.add(rec["action_id"])
            except (json.JSONDecodeError, KeyError):
                continue

    def already(self, aid: str) -> bool:
        return aid in self._seen

    def record(self, result: ExecutionResult) -> None:
        if result.status == "executed":
            self._seen.add(result.action_id)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(result.model_dump_json() + "\n")


class ActionExecutor:
    """Deterministic execution against the simulator control plane."""

    def __init__(self, client: SimClient, ledger: ActionLedger):
        self.client = client
        self.ledger = ledger

    async def execute(self, run_id: str, action: PlaybookAction) -> ExecutionResult:
        aid = action_id(run_id, action.action, action.target)
        if self.ledger.already(aid):
            return ExecutionResult(
                action_id=aid,
                action=action.action,
                target=action.target,
                status="skipped_duplicate",
                detail="action already executed for this run (idempotent resume)",
            )
        try:
            detail = await self.client.dispatch(action.action, action.params)
        except Exception as exc:  # noqa: BLE001 — surface any transport fault
            return ExecutionResult(
                action_id=aid, action=action.action, target=action.target,
                status="failed", detail=f"action plane error: {exc}",
            )
        result = ExecutionResult(
            action_id=aid, action=action.action, target=action.target,
            status="executed", detail=detail,
        )
        self.ledger.record(result)
        return result
