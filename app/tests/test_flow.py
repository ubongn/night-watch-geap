"""End-to-end Night Watch flow tests on the fake data plane + scripted LLM."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

import night_watch.graph as graph_mod
import night_watch.runtime as runtime_mod
from night_watch.model_provider import ScriptedLlm
from night_watch.runtime import RunManager

JAM_ALERT = {
    "alerts": [
        {
            "name": "ConveyorJamCritical",
            "service": "sorter-conveyor",
            "severity": "critical",
            "value": 260.0,
            "threshold": 60.0,
            "labels": {"dock": "dock-3"},
            "fingerprint": "fp-jam-1",
        }
    ]
}

DIAG_OK = json.dumps({
    "root_cause_class": "conveyor_jam", "summary": "jam at dock-3",
    "confidence": 0.9, "blast_radius": "dock-3", "evidence_refs": [],
    "recommended_action": "clear_jam"})


def _patch_runtime_deps(monkeypatch, make_deps):
    monkeypatch.setattr(runtime_mod, "_build_deps", lambda run_id: make_deps(run_id))


def _patch_scripts(monkeypatch, scripts):
    monkeypatch.setattr(graph_mod, "build_model", lambda provider=None: ScriptedLlm(scripts))


async def _drive_to_completion(mgr: RunManager, run_id: str, max_s: float = 30.0) -> dict:
    waited = 0.0
    while waited < max_s:
        await asyncio.sleep(0.05)
        waited += 0.05
        if mgr.runs[run_id]["status"] != "running":
            break
    return await mgr.run_status(run_id)


@pytest.mark.asyncio
async def test_full_flow_remediates_jam(monkeypatch, make_deps, world, fake_grafana, audit, memory):
    _patch_runtime_deps(monkeypatch, make_deps)
    world.inject("sorter-conveyor", "conveyor_jam")

    mgr = RunManager()
    await mgr.start_run(JAM_ALERT, run_id="test-jam")
    status = await _drive_to_completion(mgr, "test-jam")

    assert status["status"] == "completed", status
    assert status["outcome"] == "remediated"
    assert status["action_executed"] == "executed"
    assert [a["action"] for a in world.actions] == ["clear_jam"]
    ok, why = audit.verify()
    assert ok, why
    events = [r["event"] for r in audit.records]
    for expected in ("diagnosis", "gate", "execution", "incident_record"):
        assert expected in events, events
    hits = memory.query("sorter-conveyor", "conveyor_jam", limit=5)
    assert hits and hits[0]["outcome"] == "remediated"
    assert fake_grafana.annotations and "remediated" in fake_grafana.annotations[-1]["text"]


@pytest.mark.asyncio
async def test_quiet_webhook_no_action(monkeypatch, make_deps, world):
    _patch_runtime_deps(monkeypatch, make_deps)
    mgr = RunManager()
    await mgr.start_run({"alerts": []}, run_id="test-quiet")
    status = await _drive_to_completion(mgr, "test-quiet")
    assert status["status"] == "completed"
    assert status["outcome"] == "no_action"
    assert world.actions == []


@pytest.mark.asyncio
async def test_low_confidence_diagnosis_refuses(monkeypatch, make_deps, world):
    _patch_runtime_deps(monkeypatch, make_deps)
    world.inject("sorter-conveyor", "conveyor_jam")
    _patch_scripts(monkeypatch, [
        ("gathered_at", json.dumps({
            "root_cause_class": "conveyor_jam", "summary": "weak evidence",
            "confidence": 0.3, "blast_radius": "unknown", "evidence_refs": [],
            "recommended_action": "clear_jam"})),
        ("recommended_action", json.dumps({
            "action": "clear_jam", "target": "dock-3", "params": {"dock": "dock-3"},
            "rationale": "try it", "risk": "low"})),
    ])
    mgr = RunManager()
    await mgr.start_run(JAM_ALERT, run_id="test-lowconf")
    status = await _drive_to_completion(mgr, "test-lowconf")
    assert status["status"] == "completed"
    assert status["outcome"] == "refused"
    assert world.actions == []


@pytest.mark.asyncio
async def test_hallucinated_action_dies_at_gate(monkeypatch, make_deps, world):
    """Proposal with an invalid target must be refused by the deterministic gate."""
    _patch_runtime_deps(monkeypatch, make_deps)
    world.inject("sorter-conveyor", "conveyor_jam")
    _patch_scripts(monkeypatch, [
        ("gathered_at", DIAG_OK),
        ("recommended_action", json.dumps({
            "action": "clear_jam", "target": "dock-99", "params": {"dock": "dock-99"},
            "rationale": "hallucinated dock id", "risk": "low"})),
    ])
    mgr = RunManager()
    await mgr.start_run(JAM_ALERT, run_id="test-halluc")
    status = await _drive_to_completion(mgr, "test-halluc")
    assert status["status"] == "completed"
    assert status["outcome"] == "refused"
    assert world.actions == [], "hallucinated target must never reach the action plane"


@pytest.mark.asyncio
async def test_high_risk_requires_approval(monkeypatch, make_deps, world):
    _patch_runtime_deps(monkeypatch, make_deps)
    world.inject("sorter-conveyor", "conveyor_jam")
    _patch_scripts(monkeypatch, [
        ("gathered_at", DIAG_OK),
        ("recommended_action", json.dumps({
            "action": "clear_jam", "target": "dock-3", "params": {"dock": "dock-3"},
            "rationale": "needed", "risk": "high"})),
    ])
    monkeypatch.setattr(
        runtime_mod, "SETTINGS",
        dataclasses.replace(runtime_mod.SETTINGS, approval_policy="require_approval"),
    )
    mgr = RunManager()
    await mgr.start_run(JAM_ALERT, run_id="test-hold")
    status = await _drive_to_completion(mgr, "test-hold")
    assert status["status"] == "completed"
    assert status["outcome"] == "escalated"
    assert world.actions == []
