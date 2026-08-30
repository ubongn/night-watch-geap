"""The Night Watch gateway — the service that runs on Cloud Run.

Responsibilities:
  * ingest alert webhooks (POST /alerts) behind Model Armor screening — the
    untrusted-input lane; nothing reaches an agent unscreened
  * expose run lifecycle: status, kill, resume (the mid-incident kill is a
    first-class API, not a crash)
  * expose the durable evidence: audit chain verification + tail, incident
    memory stats, run journal
  * serve a minimal operator dashboard at / (what the on-call human sees)

Identity note: the gateway itself carries the `ingest:webhook` + `armor:screen`
scopes from night_watch.identity — it may accept telemetry and screen it, and
nothing else. It never proposes or executes actions.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import telemetry
from .armor import screen_webhook, screen_with_model_armor
from .audit import AuditChain
from .config import SETTINGS
from .grafana import SnapshotGrafana
from .identity import get_identity
from .memory import MemoryBank
from .runtime import RunManager
from .sim_client import SimClient

MANAGER: RunManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MANAGER
    telemetry.setup()
    MANAGER = RunManager()
    yield
    for run_id, task in list(MANAGER.tasks.items()):
        if not task.done():
            task.cancel()
    MANAGER = None


app = FastAPI(title="Night Watch", version="1.0.0", lifespan=lifespan)


def _mgr() -> RunManager:
    if MANAGER is None:  # pragma: no cover — lifespan always runs first
        raise HTTPException(status_code=503, detail="starting up")
    return MANAGER


# ---------------------------------------------------------------------------
# ingest (the Armor lane)
# ---------------------------------------------------------------------------


@app.post("/alerts", status_code=202)
async def receive_alert(request: Request):
    """Ingest one alert webhook. Screened before any agent sees it."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body must be JSON")

    if SETTINGS.model_armor_template:
        verdict = await screen_with_model_armor(
            payload, SETTINGS.model_armor_template,
            SETTINGS.gcp_project, SETTINGS.gcp_location,
        )
    else:
        verdict = screen_webhook(payload)

    if verdict.verdict == "block":
        # Rejected at the gate: no run, no LLM turn, but the attempt is audited.
        audit = AuditChain(SETTINGS.audit_dir / "chain.jsonl")
        audit.append(
            "inbound_blocked", "gateway",
            {"reasons": verdict.reasons[:5], "policy": "model_armor_screen"},
        )
        return JSONResponse(
            status_code=400,
            content={"verdict": "block", "reasons": verdict.reasons[:5]},
        )

    started = await _mgr().start_run(payload)
    return {"run_id": started["run_id"], "status": started["status"], "screen": "allow"}


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------


@app.get("/runs")
async def list_runs():
    return {"runs": [await _mgr().run_status(r) for r in _known_run_ids()]}


@app.get("/runs/{run_id}")
async def run_detail(run_id: str):
    status = await _mgr().run_status(run_id)
    if "error" in status:
        raise HTTPException(status_code=404, detail=status["error"])
    doc = _mgr()._read_run_state(run_id) or {}
    record = doc.get("state", {}).get("incident_record")
    return {**status, "incident_record": record}


@app.post("/runs/{run_id}/kill")
async def kill_run(run_id: str):
    out = await _mgr().kill_run(run_id)
    if "error" in out:
        raise HTTPException(status_code=404, detail=out["error"])
    return out


@app.post("/runs/{run_id}/resume")
async def resume_run(run_id: str):
    out = await _mgr().resume_run(run_id)
    if "error" in out:
        raise HTTPException(status_code=404, detail=out["error"])
    return out


def _known_run_ids() -> list[str]:
    """Union of in-memory runs and durable run-state documents on disk."""
    ids = set(MANAGER.runs.keys()) if MANAGER else set()
    if SETTINGS.run_state_dir.exists():
        for p in SETTINGS.run_state_dir.glob("run-*.json"):
            ids.add(p.stem[len("run-"):])
    return sorted(ids)


# ---------------------------------------------------------------------------
# durable evidence surfaces
# ---------------------------------------------------------------------------


@app.get("/audit")
async def audit_tail(limit: int = 50):
    chain = AuditChain(SETTINGS.audit_dir / "chain.jsonl")
    ok, why = chain.verify()
    records = [
        {"event": r.get("event"), "run_id": r.get("run_id"),
         "at_hash": r.get("hash", "")[:12], "data": r.get("data", {})}
        for r in chain.records[-limit:]
    ]
    return {"verified": ok, "verify_detail": why, "records": records}


@app.get("/audit/verify")
async def audit_verify():
    chain = AuditChain(SETTINGS.audit_dir / "chain.jsonl")
    ok, why = chain.verify()
    return {"verified": ok, "detail": why}


@app.get("/memory/stats")
async def memory_stats():
    bank = MemoryBank(SETTINGS.memory_dir / "incidents.jsonl")
    return bank.stats()


@app.get("/annotations")
async def annotations():
    grafana = SnapshotGrafana()
    return {"annotations": grafana.annotations()[-20:]}


# NOTE: on *.run.app the Google Front End reserves /healthz for its own health
# checks and 404s external requests to that exact path before they reach the
# container. /health is the public-facing alias (same payload); /healthz stays
# registered for internal/liveness use.
async def _health_payload() -> dict:
    sim = SimClient()
    chain = AuditChain(SETTINGS.audit_dir / "chain.jsonl")
    ok, why = chain.verify()
    identity = get_identity("gateway")
    return {
        "ok": True,
        "service": "night-watch",
        "provider": SETTINGS.ai_provider,
        "model": SETTINGS.gemini_model,
        "sim_reachable": await sim.health(),
        "audit_chain": {"verified": ok, "detail": why},
        "gateway_scopes": sorted(identity.scopes) if identity else [],
        "approval_policy": SETTINGS.approval_policy,
    }


@app.get("/healthz")
async def healthz():
    return await _health_payload()


@app.get("/health")
async def health():
    return await _health_payload()


# ---------------------------------------------------------------------------
# operator dashboard (what Adaeze sees at 03:47)
# ---------------------------------------------------------------------------


@app.get("/api/overview")
async def overview():
    mgr = _mgr()
    runs = []
    for rid in _known_run_ids():
        st = await mgr.run_status(rid)
        if "error" not in st:
            runs.append(st)
    chain = AuditChain(SETTINGS.audit_dir / "chain.jsonl")
    ok, why = chain.verify()
    bank = MemoryBank(SETTINGS.memory_dir / "incidents.jsonl")
    grafana = SnapshotGrafana()
    return {
        "runs": runs[-12:],
        "audit": {
            "verified": ok,
            "detail": why,
            "count": len(chain.records),
            "tail": [
                {"event": r.get("event"), "run_id": r.get("run_id"),
                 "hash": r.get("hash", "")[:12]}
                for r in chain.records[-10:]
            ],
        },
        "memory": bank.stats(),
        "annotations": grafana.annotations()[-5:],
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Night Watch — Meridian Freight</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20viewBox=%220%200%2064%2064%22%20role=%22img%22%20aria-label=%22Night%20Watch%20mark%22%3E%3Crect%20x=%220.75%22%20y=%220.75%22%20width=%2262.5%22%20height=%2262.5%22%20rx=%2214%22%20fill=%22%230b1220%22%20stroke=%22%23263154%22%20stroke-width=%221.5%22/%3E%3Ccircle%20cx=%2213.5%22%20cy=%2212.5%22%20r=%221.1%22%20fill=%22%23dfe7ff%22%20opacity=%220.55%22/%3E%3Ccircle%20cx=%2251%22%20cy=%2247%22%20r=%221.2%22%20fill=%22%23dfe7ff%22%20opacity=%220.4%22/%3E%3Ccircle%20cx=%2249.5%22%20cy=%2219.5%22%20r=%220.8%22%20fill=%22%23dfe7ff%22%20opacity=%220.5%22/%3E%3Ccircle%20cx=%2232%22%20cy=%2232%22%20r=%2226%22%20fill=%22none%22%20stroke=%22%23dfe7ff%22%20stroke-opacity=%220.28%22%20stroke-width=%221.5%22/%3E%3Cg%20stroke=%22%23dfe7ff%22%20stroke-opacity=%220.5%22%20stroke-width=%222%22%20stroke-linecap=%22round%22%3E%3Cline%20x1=%2232%22%20y1=%226%22%20x2=%2232%22%20y2=%229.6%22/%3E%3Cline%20x1=%2232%22%20y1=%2258%22%20x2=%2232%22%20y2=%2254.4%22/%3E%3Cline%20x1=%226%22%20y1=%2232%22%20x2=%229.6%22%20y2=%2232%22/%3E%3Cline%20x1=%2258%22%20y1=%2232%22%20x2=%2254.4%22%20y2=%2232%22/%3E%3C/g%3E%3Cpath%20d=%22M32%206%20A26%2026%200%200%201%2050.4%2013.6%22%20fill=%22none%22%20stroke=%22%23f5b942%22%20stroke-width=%222.6%22%20stroke-linecap=%22round%22/%3E%3Ccircle%20cx=%2250.4%22%20cy=%2213.6%22%20r=%222.7%22%20fill=%22%23f5b942%22/%3E%3Cpath%20fill=%22%23dfe7ff%22%20d=%22M26.32%2021.25%20A15%2015%200%201%200%2043.34%2040.39%20A13.5%2013.5%200%200%201%2026.32%2021.25%20Z%22/%3E%3Cpath%20d=%22M32%2027Q39.5%2020.3%2047%2027Q39.5%2033.7%2032%2027Z%22%20fill=%22%23dfe7ff%22/%3E%3Ccircle%20cx=%2239.5%22%20cy=%2227%22%20r=%223.4%22%20fill=%22%230b1220%22/%3E%3Ccircle%20cx=%2241.1%22%20cy=%2225.5%22%20r=%221%22%20fill=%22%23f5b942%22/%3E%3C/svg%3E">
<style>
:root{--bg:#0b1020;--panel:#131a30;--line:#2a3558;--ink:#dbe4ff;--dim:#8fa3d9;--acc:#ffb454;--ok:#3ddc84;--bad:#ff5c7a}
body{font-family:ui-monospace,Consolas,monospace;background:var(--bg);color:var(--ink);margin:0;padding:1.6rem}
h1{margin:0 0 .2rem;font-size:1.35rem;color:var(--acc)}
h2{font-size:.95rem;color:var(--dim);margin:1.4rem 0 .5rem;text-transform:uppercase;letter-spacing:.08em}
sub{color:var(--dim)}
.brand{display:flex;align-items:center;gap:.7rem;margin-bottom:.3rem}
.brand svg{flex:none;border-radius:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
table{border-collapse:collapse;width:100%}td,th{padding:.35rem .6rem;border-bottom:1px solid var(--line);text-align:left;font-size:.85rem}
th{color:var(--dim);font-weight:400}
.badge{display:inline-block;padding:.1rem .55rem;border-radius:99px;font-size:.75rem}
.b-ok{background:rgba(61,220,132,.15);color:var(--ok)}.b-bad{background:rgba(255,92,122,.15);color:var(--bad)}
.b-run{background:rgba(255,180,84,.15);color:var(--acc)}
.map{white-space:pre;font-size:.8rem;line-height:1.5;color:var(--dim)}
.map b{color:var(--ink)}
footer{margin-top:1.5rem;color:var(--dim);font-size:.75rem}
</style></head><body>
<div class="brand"><svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 64 64" role="img" aria-label="Night Watch mark"><rect x="0.75" y="0.75" width="62.5" height="62.5" rx="14" fill="#0b1220" stroke="#263154" stroke-width="1.5"/><circle cx="13.5" cy="12.5" r="1.1" fill="#dfe7ff" opacity="0.55"/><circle cx="51" cy="47" r="1.2" fill="#dfe7ff" opacity="0.4"/><circle cx="49.5" cy="19.5" r="0.8" fill="#dfe7ff" opacity="0.5"/><circle cx="32" cy="32" r="26" fill="none" stroke="#dfe7ff" stroke-opacity="0.28" stroke-width="1.5"/><g stroke="#dfe7ff" stroke-opacity="0.5" stroke-width="2" stroke-linecap="round"><line x1="32" y1="6" x2="32" y2="9.6"/><line x1="32" y1="58" x2="32" y2="54.4"/><line x1="6" y1="32" x2="9.6" y2="32"/><line x1="58" y1="32" x2="54.4" y2="32"/></g><path d="M32 6 A26 26 0 0 1 50.4 13.6" fill="none" stroke="#f5b942" stroke-width="2.6" stroke-linecap="round"/><circle cx="50.4" cy="13.6" r="2.7" fill="#f5b942"/><path fill="#dfe7ff" d="M26.32 21.25 A15 15 0 1 0 43.34 40.39 A13.5 13.5 0 0 1 26.32 21.25 Z"/><path d="M32 27Q39.5 20.3 47 27Q39.5 33.7 32 27Z" fill="#dfe7ff"/><circle cx="39.5" cy="27" r="3.4" fill="#0b1220"/><circle cx="41.1" cy="25.5" r="1" fill="#f5b942"/></svg><div><h1>Night Watch</h1><sub>five-agent SRE fleet &mdash; Meridian Freight night shift &middot; gateway / armor / registry &middot; <span id="prov"></span></sub></div></div>
<div class="grid">
<div class="panel"><h2>Fleet (one ADK 2 graph)</h2><div class="map">START &rarr; <b>Detector</b> &rarr; Evidence &rarr; <b>Diagnostician</b> (LLM)
  &rarr; <b>Remediator</b> (LLM) &rarr; <b>PolicyGate</b>
    &rarr; execute: <b>Executor</b> &rarr; PostEvidence &rarr; <b>Verifier</b> (LLM)
    &rarr; refuse / hold &rarr; <b>Scribe</b> (audit + memory)</div></div>
<div class="panel"><h2>Posture</h2><table>
<tr><td>audit chain</td><td id="chain"></td></tr>
<tr><td>records</td><td id="acount"></td></tr>
<tr><td>incidents remembered</td><td id="mcount"></td></tr>
<tr><td>median MTTR</td><td id="mttr"></td></tr>
</table></div>
</div>
<h2>Runs</h2><div class="panel"><table id="runs"><tr><th>run</th><th>status</th><th>attempts</th><th>outcome</th><th>action</th><th>nodes</th></tr></table></div>
<h2>Audit tail</h2><div class="panel"><table id="audit"><tr><th>event</th><th>run</th><th>hash</th></tr></table></div>
<footer>POST /alerts to start a run &middot; POST /runs/{id}/kill &middot; POST /runs/{id}/resume &middot; GET /audit/verify</footer>
<script>
const esc=s=>String(s??'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
async function tick(){
 try{
  const h=await (await fetch('/health')).json();
  document.getElementById('prov').textContent='provider: '+esc(h.provider);
  const o=await (await fetch('/api/overview')).json();
  const chain=document.getElementById('chain');
  chain.innerHTML=o.audit.verified?'<span class="badge b-ok">verified</span>':'<span class="badge b-bad">BROKEN</span>';
  document.getElementById('acount').textContent=o.audit.count;
  document.getElementById('mcount').textContent=o.memory.incidents_remembered;
  document.getElementById('mttr').textContent=o.memory.median_mttr_s!=null?o.memory.median_mttr_s+'s':'—';
  const rt=document.getElementById('runs');
  rt.innerHTML='<tr><th>run</th><th>status</th><th>attempts</th><th>outcome</th><th>action</th><th>nodes</th></tr>'+
   (o.runs.slice().reverse().map(r=>'<tr><td>'+esc(r.run_id)+'</td><td><span class="badge '+(r.status==='completed'?'b-ok':r.status==='running'?'b-run':'b-bad')+'">'+esc(r.status)+'</span></td><td>'+esc(r.attempts)+'</td><td>'+esc(r.outcome||'—')+'</td><td>'+esc(r.action_executed||'—')+'</td><td>'+esc((r.nodes_seen||[]).length)+'</td></tr>').join('')||'<tr><td colspan="6">quiet night — no runs yet</td></tr>');
  const at=document.getElementById('audit');
  at.innerHTML='<tr><th>event</th><th>run</th><th>hash</th></tr>'+
   o.audit.tail.slice().reverse().map(a=>'<tr><td>'+esc(a.event)+'</td><td>'+esc(a.run_id)+'</td><td>'+esc(a.hash)+'</td></tr>').join('');
 }catch(e){console.error(e)}
}
tick();setInterval(tick,3000);
</script></body></html>"""
