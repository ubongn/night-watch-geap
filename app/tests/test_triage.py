"""Gemma triage tier tests: parsing, model fallback, fail-open, integration.

The tier's contract (all must hold):
  * parse: clean / fenced / preamble-wrapped Gemma turns all classify
  * fallback: a rejected model string moves down the chain
  * fail-open: ANY error or timeout degrades — never raises, never blocks
  * off: flag off (or scripted provider) → no triage at all, flow unchanged
  * integration: a real run carries triage context + an audit `triage` entry,
    and a hanging Gemma still ends remediated (fail-open end-to-end)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

import night_watch.graph as graph_mod
import night_watch.runtime as runtime_mod
import night_watch.server as server_mod
import night_watch.triage as triage_mod
from night_watch.model_provider import ScriptedLlm
from night_watch.triage import (
    GemmaTriage,
    TriageHandle,
    TriageResult,
    _parse_json,
    _validate,
)
from night_watch.server import app

from tests.test_flow import DIAG_OK, JAM_ALERT, _patch_runtime_deps

GOOD_JSON = json.dumps({
    "severity_band": "critical", "service": "sorter-conveyor",
    "fault_class": "conveyor_jam", "duplicate": False,
    "known_incident": None, "confidence": 0.9, "rationale": "jam threshold blown",
})


class FakeAio:
    def __init__(self, replies: dict[str, object]):
        self.replies = replies
        self.calls: list[str] = []

    async def generate_content(self, model: str, contents: str, config=None):
        self.calls.append(model)
        reply = self.replies[model]
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"text": reply})()


def make_triage(replies: dict[str, object]) -> GemmaTriage:
    t = GemmaTriage("test-key", list(replies))
    t._aio_models = FakeAio(replies)
    return t


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_parse_clean_json():
    parsed = _validate(_parse_json(GOOD_JSON))
    assert parsed["severity_band"] == "critical"
    assert parsed["fault_class"] == "conveyor_jam"


def test_parse_fenced_json():
    parsed = _parse_json("```json\n" + GOOD_JSON + "\n```")
    assert parsed["service"] == "sorter-conveyor"


def test_parse_json_with_preamble():
    parsed = _parse_json("Here is the classification you asked for: " + GOOD_JSON)
    assert parsed["confidence"] == 0.9


def test_parse_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_json("the conveyor is probably fine, no JSON here")


def test_validate_requires_keys():
    with pytest.raises(ValueError):
        _validate({"severity_band": "high"})


# ---------------------------------------------------------------------------
# model chain + fail-open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_chain_falls_through_rejections():
    """404 on gemma-3-27b-it -> 12b also gone -> gemma-4 answers."""
    tri = make_triage({
        "gemma-3-27b-it": RuntimeError("404 NOT_FOUND model not found"),
        "gemma-3-12b-it": RuntimeError("404 NOT_FOUND model not found"),
        "gemma-4-26b-a4b-it": GOOD_JSON,
    })
    out = await tri.classify(JAM_ALERT, [])
    assert out.degraded is False
    assert out.model == "gemma-4-26b-a4b-it"
    assert out.classification["severity_band"] == "critical"


@pytest.mark.asyncio
async def test_all_models_fail_degrades_never_raises():
    tri = make_triage({
        "gemma-3-27b-it": RuntimeError("429 quota"),
        "gemma-4-26b-a4b-it": ValueError("boom"),
    })
    out = await tri.classify(JAM_ALERT, [])
    assert out.degraded is True
    assert "429" in out.reason or "boom" in out.reason
    assert out.classification is None


@pytest.mark.asyncio
async def test_unparseable_turn_degrades_without_retry():
    """A model that answers gibberish degrades immediately (no chain burn)."""
    tri = make_triage({
        "gemma-3-27b-it": "sorry, I cannot classify this",
        "gemma-4-26b-a4b-it": GOOD_JSON,
    })
    out = await tri.classify(JAM_ALERT, [])
    assert out.degraded is True
    assert "unparseable" in out.reason.lower() or "JSON" in out.reason
    assert tri._aio_models.calls == ["gemma-3-27b-it"]


@pytest.mark.asyncio
async def test_handle_timeout_degrades_without_cancelling_task():
    """A slow Gemma misses the budget -> degraded on time; task unharmed."""

    async def slow():
        await asyncio.sleep(1.0)
        return TriageResult(model="gemma-4-26b-a4b-it", classification={"service": "x"})

    task = asyncio.create_task(slow())
    handle = TriageHandle(task, budget_s=0.05)
    t0 = time.monotonic()
    out = await handle.result()
    assert out.degraded is True
    assert "timeout" in out.reason
    assert time.monotonic() - t0 < 0.5, "handle must return at the deadline, not after the task"
    await task  # shielded: the call itself was never cancelled
    assert task.result().classification == {"service": "x"}


def test_flag_and_provider_gate_start():
    """Scripted provider / flag off -> no handle, no task, nothing changes."""
    assert triage_mod.enabled() is False  # AI_PROVIDER=fake in tests
    assert triage_mod.start_triage(JAM_ALERT) is None


def test_start_triage_never_raises(monkeypatch, tmp_path):
    """Even a broken memory bank / raising internals degrade to None."""
    new = dataclasses.replace(
        runtime_mod.SETTINGS, ai_provider="gemini", gemini_api_key="k",
        memory_dir=tmp_path / "nope", gemma_triage_models=",,",  # degenerate chain
    )
    monkeypatch.setattr(triage_mod, "SETTINGS", new)
    handle = triage_mod.start_triage(JAM_ALERT)
    # chain degenerates to the default model; handle exists but its classify
    # would degrade — and the handle itself is still returned harmlessly
    assert handle is None or isinstance(handle, TriageHandle)


# ---------------------------------------------------------------------------
# integration (through the real gateway + graph)
# ---------------------------------------------------------------------------

class StubTriager:
    def __init__(self, result: TriageResult):
        self.result = result

    async def classify(self, payload, memory_hits=None):
        if self.result.degraded and "hang" in self.result.reason:
            await asyncio.sleep(30)
        return self.result


@pytest.fixture()
def server_dirs(tmp_path, monkeypatch):
    new = dataclasses.replace(
        runtime_mod.SETTINGS,
        run_state_dir=tmp_path / "run-state",
        audit_dir=tmp_path / "audit",
        memory_dir=tmp_path / "memory",
    )
    monkeypatch.setattr(runtime_mod, "SETTINGS", new)
    monkeypatch.setattr(server_mod, "SETTINGS", new)
    return new


def _force_triage_on(monkeypatch, stub: StubTriager):
    monkeypatch.setattr(triage_mod, "SETTINGS", dataclasses.replace(
        runtime_mod.SETTINGS, ai_provider="gemini", gemini_api_key="test-key",
        gemma_triage="on", gemma_triage_timeout_s=0.5,
    ))
    monkeypatch.setattr(triage_mod, "enabled", lambda: True)
    monkeypatch.setattr(triage_mod, "_TRIAGER", stub)


def _scripts():
    return [
        ("prior_diagnosis", json.dumps({"verdict": "verified", "summary": "ok",
                                        "evidence_refs": [], "confidence": 0.9})),
        ("gathered_at", DIAG_OK),
        ("recommended_action", json.dumps({"action": "clear_jam", "target": "dock-3",
                                           "params": {"dock": "dock-3"},
                                           "rationale": "clear it", "risk": "low"})),
    ]


def test_triage_context_rides_the_run_and_audits(monkeypatch, make_deps, world, audit, server_dirs):
    _patch_runtime_deps(monkeypatch, make_deps)
    monkeypatch.setattr(graph_mod, "build_model", lambda provider=None: ScriptedLlm(_scripts()))
    _force_triage_on(monkeypatch, StubTriager(TriageResult(
        model="gemma-4-26b-a4b-it",
        classification={"severity_band": "critical", "service": "sorter-conveyor",
                        "fault_class": "conveyor_jam", "duplicate": False,
                        "known_incident": None, "confidence": 0.9, "rationale": "jam"},
    )))
    world.inject("sorter-conveyor", "conveyor_jam")

    with TestClient(app) as client:
        resp = client.post("/alerts", json=JAM_ALERT)
        assert resp.status_code == 202
        assert resp.json()["triage"] == "gemma (in-flight, fail-open)"
        run_id = resp.json()["run_id"]

        deadline = time.time() + 30
        status = {}
        while time.time() < deadline:
            status = client.get(f"/runs/{run_id}").json()
            if status.get("status") != "running":
                break
            time.sleep(0.1)

        assert status["status"] == "completed", status
        assert status["outcome"] == "remediated"
        # triage context reached the Diagnostician's evidence bundle (run state)
        doc = server_mod.MANAGER._read_run_state(run_id) or {}
        ev = (doc.get("state") or {}).get("evidence") or {}
        assert (ev.get("triage") or {}).get("model") == "gemma-4-26b-a4b-it"
        assert ev["triage"]["severity_band"] == "critical"
        # and the tier is on the audit chain, hash-linked (the run's chain —
        # in production deps.audit and the gateway /audit file are one file)
        assert audit.verify()[0] is True
        triage_entries = [r for r in audit.records if r["event"] == "triage"]
        assert len(triage_entries) == 1
        entry = triage_entries[0]["data"]
        assert entry["agent"] == "triage"
        assert entry["model"] == "gemma-4-26b-a4b-it"
        assert entry["status"] == "ok"
        assert entry["classification"]["severity_band"] == "critical"


def test_hanging_gemma_never_blocks_the_run(monkeypatch, make_deps, world, audit, server_dirs):
    """Fail-open end-to-end: Gemma hangs past the budget; run still remediates."""
    _patch_runtime_deps(monkeypatch, make_deps)
    monkeypatch.setattr(graph_mod, "build_model", lambda provider=None: ScriptedLlm(_scripts()))
    _force_triage_on(monkeypatch, StubTriager(TriageResult(
        degraded=True, reason="hang: simulated stuck model",
    )))
    world.inject("sorter-conveyor", "conveyor_jam")

    with TestClient(app) as client:
        t0 = time.time()
        resp = client.post("/alerts", json=JAM_ALERT)
        assert resp.status_code == 202
        assert time.time() - t0 < 5, "webhook must not wait on the hung tier"

        run_id = resp.json()["run_id"]
        deadline = time.time() + 30
        status = {}
        while time.time() < deadline:
            status = client.get(f"/runs/{run_id}").json()
            if status.get("status") != "running":
                break
            time.sleep(0.1)

        assert status["status"] == "completed", status
        assert status["outcome"] == "remediated"
        assert audit.verify()[0] is True
        entry = next(r for r in audit.records if r["event"] == "triage")["data"]
        assert entry["status"] == "degraded"
        assert "timeout" in entry["reason"]
