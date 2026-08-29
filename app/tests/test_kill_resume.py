"""The flagship twist: kill an agent mid-incident and resume it.

A run is killed after the action executed but before verification. Resume
re-drives the workflow; the action ledger refuses the duplicate dispatch, so
the physical action happens exactly once, and the run still completes with a
verified outcome.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

import night_watch.runtime as runtime_mod
from night_watch.runtime import RunManager

from tests.test_flow import DIAG_OK, JAM_ALERT, _patch_runtime_deps

SLOW_VERIFY = [
    ("prior_diagnosis", json.dumps({
        "verdict": "verified", "summary": "jam cleared; throughput recovered",
        "evidence_refs": [], "confidence": 0.9})),
    ("gathered_at", DIAG_OK),
    ("recommended_action", json.dumps({
        "action": "clear_jam", "target": "dock-3", "params": {"dock": "dock-3"},
        "rationale": "clear the jam", "risk": "low"})),
]


@pytest.mark.asyncio
async def test_kill_mid_incident_and_resume(monkeypatch, make_deps, world, audit):
    _patch_runtime_deps(monkeypatch, make_deps)
    monkeypatch.setattr(
        runtime_mod, "SETTINGS",
        dataclasses.replace(runtime_mod.SETTINGS, verify_cooldown_s=8.0),
    )
    world.inject("sorter-conveyor", "conveyor_jam")

    import night_watch.graph as graph_mod
    from night_watch.model_provider import ScriptedLlm
    monkeypatch.setattr(graph_mod, "build_model", lambda provider=None: ScriptedLlm(SLOW_VERIFY))

    mgr = RunManager()
    await mgr.start_run(JAM_ALERT, run_id="test-kr")

    # wait until the action has executed, then kill during the verify cooldown
    for _ in range(400):
        await asyncio.sleep(0.05)
        if world.actions:
            break
    assert world.actions, "action should have executed before the kill"
    killed = await mgr.kill_run("test-kr")
    assert killed["status"] == "killed"

    # ledger has exactly one committed action
    status = await mgr.run_status("test-kr")
    assert status["status"] == "killed"
    assert status["outcome"] is None, "killed run must not have a final outcome yet"

    # -- resume ----------------------------------------------------------
    resumed = await mgr.resume_run("test-kr")
    assert resumed["resumed"] is True

    waited = 0.0
    while waited < 40.0:
        await asyncio.sleep(0.1)
        waited += 0.1
        if mgr.runs["test-kr"]["status"] != "running":
            break
    final = await mgr.run_status("test-kr")

    assert final["status"] == "completed", final
    assert final["attempts"] == 2
    # THE idempotency claim: one physical action across kill+resume
    assert [a["action"] for a in world.actions] == ["clear_jam"], world.actions
    # and the run still closed verified
    assert final["outcome"] == "remediated"
    ok, why = audit.verify()
    assert ok, why


@pytest.mark.asyncio
async def test_ledger_blocks_duplicate_execution(monkeypatch, ledger, world):
    """Direct ledger behavior: same run+action+target executes once."""
    from night_watch.actions import ActionExecutor
    from night_watch.models import PlaybookAction

    ex = ActionExecutor(world, ledger)
    proposal = PlaybookAction(action="clear_jam", target="dock-3", params={"dock": "dock-3"})
    r1 = await ex.execute("run-x", proposal)
    r2 = await ex.execute("run-x", proposal)
    assert r1.status == "executed"
    assert r2.status == "skipped_duplicate"
    assert len(world.actions) == 1


@pytest.mark.asyncio
async def test_audit_detects_tampering(audit):
    audit.append("diagnosis", "run-t", {"a": 1})
    audit.append("gate", "run-t", {"b": 2})
    ok, _ = audit.verify()
    assert ok
    # retroactively edit a record
    audit.records[0]["data"] = {"a": 999}
    ok, why = audit.verify()
    assert not ok and "tampered" in why
