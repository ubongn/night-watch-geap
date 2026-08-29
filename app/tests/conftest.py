"""Shared test fixtures: a fake Grafana data plane and a fake action plane.

The fake Grafana is a small mutable "world": fault registry + metric/log
generators that respond to control actions, so evidence before remediation
shows the fault and evidence after shows recovery.
"""

from __future__ import annotations

import pytest

from night_watch import deps as deps_mod
from night_watch.actions import ActionExecutor, ActionLedger
from night_watch.audit import AuditChain
from night_watch.grafana import GrafanaClient
from night_watch.memory import MemoryBank
from night_watch.models import LogExcerpt, MetricSeries


class FakeWorld:
    """Deterministic telemetry world driven by an injected fault."""

    def __init__(self):
        self.faults: dict[str, dict] = {}  # service -> {"class": ..., "since": ...}
        self.actions: list[dict] = []

    def inject(self, service: str, fault_class: str):
        self.faults[service] = {"class": fault_class}

    def clear(self, service: str):
        self.faults.pop(service, None)

    # -- control plane ------------------------------------------------------

    async def dispatch(self, action: str, params: dict[str, str]) -> str:
        self.actions.append({"action": action, "params": params})
        fixes = {
            "restart_service": "api_latency",
            "clear_jam": "conveyor_jam",
            "roll_worker_pool": "db_connection_exhaustion",
            "throttle_ingest": "ingest_backlog",
            "drain_dock": "dock_fault",
        }
        fixed = fixes.get(action)
        for service in list(self.faults):
            if self.faults[service]["class"] == fixed:
                self.clear(service)
        return f"{action} applied to {params}"

    # -- data plane -----------------------------------------------------------

    def evidence_for(self, service: str, window_minutes: int = 10):
        fault = self.faults.get(service)
        if fault is None:
            metrics = [
                MetricSeries(query="error_rate", values=[0.01, 0.0, 0.01], unit="ratio"),
                MetricSeries(query="p99_latency", values=[220.0, 210.0, 215.0], unit="ms"),
            ]
            logs = LogExcerpt(query="{service=\"x\"}", lines=["ok: request completed"], service=service)
        elif fault["class"] == "conveyor_jam":
            metrics = [
                MetricSeries(query="meridian_conveyor_jam_seconds", values=[0, 40, 120, 260], unit="s"),
                MetricSeries(query="meridian_sorter_throughput", values=[900, 400, 90, 20], unit="items/min"),
            ]
            logs = LogExcerpt(
                query="{service=\"sorter-conveyor\"}",
                lines=["ERROR motor_overtemp dock-3", "ERROR jam_detected dock-3"],
                service=service,
            )
        elif fault["class"] == "api_latency":
            metrics = [
                MetricSeries(query="p99_latency", values=[220, 900, 2400, 3800], unit="ms"),
                MetricSeries(query="error_rate", values=[0.01, 0.08, 0.22], unit="ratio"),
            ]
            logs = LogExcerpt(
                query="{service=\"dispatch-api\"}", lines=["WARN upstream slow", "ERROR timeout"], service=service
            )
        else:
            metrics = [MetricSeries(query="saturation", values=[0.9, 0.97], unit="ratio")]
            logs = LogExcerpt(query="{service=\"x\"}", lines=[f"ERROR {fault['class']}"], service=service)
        return {"metrics": metrics, "logs": logs}


@pytest.fixture()
def world():
    return FakeWorld()


class FakeGrafana(GrafanaClient):
    """GrafanaClient bound to the fake world; annotations recorded in memory."""

    def __init__(self, world: FakeWorld):
        self._world = world
        self.annotations: list[dict] = []

    async def evidence_for(self, service: str, window_minutes: int = 10):
        return self._world.evidence_for(service, window_minutes)

    async def annotate(self, text: str, tags: list[str] | None = None):
        self.annotations.append({"text": text, "tags": tags or []})


@pytest.fixture()
def fake_grafana(world):
    return FakeGrafana(world)


@pytest.fixture()
def audit(tmp_path):
    return AuditChain(tmp_path / "chain.jsonl")


@pytest.fixture()
def memory(tmp_path):
    return MemoryBank(tmp_path / "incidents.jsonl")


@pytest.fixture()
def ledger(tmp_path):
    return ActionLedger(tmp_path / "ledger.jsonl")


@pytest.fixture()
def make_deps(world, fake_grafana, audit, memory, ledger):
    """Return a factory building RunDeps bound to the fake planes."""

    def _make(run_id: str):
        run_deps = deps_mod.RunDeps(
            grafana=fake_grafana,
            audit=audit,
            memory=memory,
            executor=ActionExecutor(world, ledger),
            ledger=ledger,
        )
        deps_mod.register(run_id, run_deps)
        return run_deps

    return _make
