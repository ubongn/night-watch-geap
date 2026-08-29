"""Run manager — create, drive, kill, and resume Night Watch runs.

Each alert webhook becomes one *run*: an ADK session plus one workflow
execution plus a durable run-state document (JSON, rewritten after every
workflow event). Kill = cancel the in-flight asyncio task (or, on Cloud Run,
the process dies). Resume = re-drive the same session with fresh live deps;
ADK's workflow resume machinery skips nodes whose outputs are already in
session state, and the action ledger on disk refuses duplicate execution
even if a node is re-entered.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .actions import ActionExecutor, ActionLedger
from .audit import AuditChain
from .config import SETTINGS
from .deps import RunDeps, register, resolve
from .grafana import GrafanaClient
from .graph import build_workflow
from .memory import MemoryBank
from .sim_client import SimClient
from . import telemetry

APP_NAME = "night-watch"
RESUMABLE_STATUSES = ("killed", "failed")


def _build_deps(run_id: str) -> RunDeps:
    """Wire the shared components for a run."""
    state_dir = SETTINGS.run_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = ActionLedger(state_dir / f"ledger-{run_id}.jsonl")
    # Grafana Cloud when credentials are configured; otherwise the simulator
    # data plane behind the same interface (local dev, CI, self-contained demo).
    if SETTINGS.prom_query_url:
        grafana = GrafanaClient()
    else:
        from .grafana import SnapshotGrafana

        grafana = SnapshotGrafana()
    return RunDeps(
        grafana=grafana,
        audit=AuditChain(SETTINGS.audit_dir / "chain.jsonl"),
        memory=MemoryBank(SETTINGS.memory_dir / "incidents.jsonl"),
        executor=ActionExecutor(SimClient(), ledger),
        ledger=ledger,
    )


class RunManager:
    def __init__(self, session_service=None):
        self.workflow = build_workflow()
        self.session_service = session_service or InMemorySessionService()
        self.runner = Runner(
            app_name=APP_NAME, agent=self.workflow, session_service=self.session_service
        )
        self.tasks: dict[str, asyncio.Task] = {}
        self.runs: dict[str, dict[str, Any]] = {}

    # -- lifecycle ----------------------------------------------------------

    async def start_run(self, alert_payload: dict, run_id: str | None = None) -> dict:
        run_id = run_id or f"nw-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        deps = _build_deps(run_id)
        register(run_id, deps)

        await self.session_service.create_session(
            app_name=APP_NAME,
            user_id="night-watch",
            session_id=run_id,
            state={
                "run_id": run_id,
                "alert_webhook": alert_payload,
                "approval_policy": SETTINGS.approval_policy,
                "verify_cooldown_s": SETTINGS.verify_cooldown_s,
            },
        )
        self.runs[run_id] = {
            "session_id": run_id,
            "status": "running",
            "started_at": time.time(),
            "attempts": 1,
            "events": [],
        }
        self._write_run_state(run_id, status="running")
        self.tasks[run_id] = asyncio.create_task(self._drive(run_id))
        return {"run_id": run_id, "status": "running"}

    async def _drive(self, run_id: str) -> None:
        info = self.runs[run_id]
        try:
            async for event in self.runner.run_async(
                user_id="night-watch",
                session_id=run_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="night watch: begin run")]
                ),
            ):
                info["events"].append({"author": event.author, "id": event.id, "ts": time.time()})
                with telemetry.tracer().start_as_current_span(f"nw.node.{event.author}") as span:
                    span.set_attribute("nw.run_id", run_id)
                    span.set_attribute("nw.node", str(event.author))
                await self._checkpoint(run_id)
            info["status"] = "completed"
            self._write_run_state(run_id, status="completed")
        except asyncio.CancelledError:
            info["status"] = "killed"
            self._write_run_state(run_id, status="killed")
            raise
        except Exception as exc:  # noqa: BLE001
            info["status"] = "failed"
            self._write_run_state(run_id, status="failed", error=str(exc)[:500])
            raise

    async def kill_run(self, run_id: str) -> dict:
        task = self.tasks.get(run_id)
        info = self.runs.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            status = "killed"
        elif info:
            status = info["status"]
        else:
            return {"run_id": run_id, "error": "unknown run"}
        self._write_run_state(run_id, status=status)
        return {"run_id": run_id, "status": status}

    async def resume_run(self, run_id: str) -> dict:
        info = self.runs.get(run_id)
        if info is None:
            # cold resume (process restarted): rebuild deps from disk
            doc = self._read_run_state(run_id)
            if doc is None:
                return {"run_id": run_id, "error": "unknown run"}
            deps = _build_deps(run_id)
            register(run_id, deps)
            session = await self.session_service.create_session(
                app_name=APP_NAME,
                user_id="night-watch",
                session_id=run_id,
                state=doc.get("state") or {"run_id": run_id},
            )
            info = self.runs[run_id] = {
                "session_id": run_id,
                "status": doc.get("status", "killed"),
                "started_at": time.time(),
                "attempts": doc.get("attempts", 1),
                "events": [],
            }
        if info["status"] not in RESUMABLE_STATUSES and info["status"] != "running":
            return {"run_id": run_id, "status": info["status"], "note": "not resumable"}
        # fresh live deps (grafana client may be closed; ledger stays on disk)
        deps = resolve(run_id) or _build_deps(run_id)
        register(run_id, deps)
        info["attempts"] += 1
        info["status"] = "running"
        self._write_run_state(run_id, status="running", resumed=True)
        self.tasks[run_id] = asyncio.create_task(self._drive(run_id))
        return {"run_id": run_id, "status": "running", "resumed": True}

    # -- introspection --------------------------------------------------------

    async def run_status(self, run_id: str) -> dict:
        info = self.runs.get(run_id)
        if info is None:
            doc = self._read_run_state(run_id)
            return doc or {"run_id": run_id, "error": "unknown run"}
        session = await self.session_service.get_session(
            app_name=APP_NAME, user_id="night-watch", session_id=run_id
        )
        state = dict(session.state or {}) if session else {}
        return {
            "run_id": run_id,
            "status": info["status"],
            "attempts": info["attempts"],
            "nodes_seen": [e["author"] for e in info["events"]],
            "outcome": (state.get("incident_record") or {}).get("outcome"),
            "action_executed": (state.get("execution") or {}).get("status"),
        }

    # -- durable state ----------------------------------------------------------

    async def _checkpoint(self, run_id: str) -> None:
        session = await self.session_service.get_session(
            app_name=APP_NAME, user_id="night-watch", session_id=run_id
        )
        if session is None:
            return
        state = dict(session.state or {})
        doc = self._read_run_state(run_id) or {"run_id": run_id}
        doc.update(
            status=self.runs[run_id]["status"],
            state=state,
            events=self.runs[run_id]["events"][-24:],
        )
        path = self._run_state_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")

    def _run_state_path(self, run_id: str) -> Path:
        return SETTINGS.run_state_dir / f"run-{run_id}.json"

    def _write_run_state(self, run_id: str, **patch) -> None:
        doc = self._read_run_state(run_id) or {"run_id": run_id}
        doc.update(patch)
        path = self._run_state_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")

    def _read_run_state(self, run_id: str) -> dict | None:
        path = self._run_state_path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
