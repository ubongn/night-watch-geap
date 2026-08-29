# Night Watch

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-2.x-4285F4)](https://google.github.io/adk-docs/)
[![Gemini](https://img.shields.io/badge/Google%20Gemini-3.5%20Flash-e8710a)](https://ai.google.dev)
[![Cloud Run](https://img.shields.io/badge/Google%20Cloud-Cloud%20Run-4285F4)](https://cloud.google.com/run)

> A five-agent SRE fleet that works the night shift — so the on-call human doesn't have to.

**Night Watch** guards the overnight operation of **Meridian Freight**, a fictional
40-person parcel-logistics company. Between 22:00 and 06:00, a single on-call engineer
(Adaeze) is responsible for five services — dispatch API, sorter conveyors, the
warehouse DB, GPS ingest, EV charge docks. Tonight, like every night, parcels must keep
moving. Night Watch watches the telemetry, diagnoses incidents, proposes and executes
playbook remediations, verifies recovery, and writes an auditable postmortem —
autonomously, with deterministic guardrails where they matter.

Built for the **All Things Agentic Hackathon** (Google). Fresh codebase; it extends the
agent-loop concepts we developed in an earlier in-house Grafana incident-response build
(disclosed per hackathon rules — see [Lineage](#lineage)).

---

## Judges: start here

| You want | Go to |
|---|---|
| What it is in 60 seconds | This page, top → "The fleet" graph below |
| Proof it works (graded) | [Evals: 9/9 matrix](#graded-results) · [`evals/results/report.md`](evals/results/report.md) |
| Proof it runs on Google Cloud | [`deploy/`](deploy/README.md) (Cloud Run, both services) · [Demo runbook](docs/demo-day.md) |
| Safety rails (injection, hallucination, kill/resume) | [The recovery story](#the-recovery-story-what-if-an-agent-loops-or-hallucinates) below |
| Lineage / reused components disclosure | [Lineage](#lineage-rule-disclosure) (bottom) |

## Why this shape

| Judging criterion | How Night Watch answers it |
|---|---|
| **Innovation & Operational Utility** | Autonomous incident response is the highest-friction overnight workflow in ops. The fleet completes detect → diagnose → remediate → verify → report with zero human turns, in the background, while the human sleeps. |
| **Architectural Discipline** | Deterministic business rules (routing, policy gates, execution, audit) are plain graph nodes; probabilistic reasoning (diagnosis, proposals, verification judgment) lives in LlmAgents behind strict output schemas. Every action passes an identity-scoped policy gate and an idempotency ledger before execution. |
| **Demo & Production Readiness** | Runs on Google Cloud Run; Grafana Cloud is the live telemetry data plane; OpenTelemetry spans per agent; the audit chain is tamper-evident and verified in tests. |

## Mapped onto the Gemini Enterprise Agent Platform surfaces

Night Watch implements the GEAP component set end-to-end:

| GEAP surface | Where it lives in Night Watch |
|---|---|
| **Agent Registry** | `night_watch/models.py` + `night_watch/identity.py` — every agent declares identity, scopes, and its closed action set; run-scoped live objects resolve through the registry in `deps.py`, nothing is hardcoded in edges. |
| **Agent Runtime** | `night_watch/graph.py` + `night_watch/runtime.py` — ADK 2 graph engine with routed edges, node state, checkpoint/kill/resume run lifecycle. |
| **Memory Bank** | `night_watch/memory.py` — incident memory (Qdrant-style retrieval interface, in-proc store by default) written by Scribe, read by Diagnostician. |
| **Identity** | `night_watch/identity.py` — per-agent credential scopes (`telemetry:read`, `actions:propose`, …) enforced at the policy gate; no agent can act outside its lane. |
| **Gateway** | `night_watch/server.py` — FastAPI ingest (`/alerts`), run lifecycle API, audit/memory surfaces, operator dashboard. |
| **Model Armor** | `night_watch/armor.py` — screening on every inbound webhook before any LLM turn (nested-payload injection caught per-alert); optional Vertex Model Armor template upstream. |
| **OpenTelemetry** | per-node OTLP spans in the runtime (`OTEL_EXPORTER_OTLP_ENDPOINT`); the local no-OTel path is a no-op, same interface. |

## The fleet (one ADK 2 graph workflow)

```
START → Detector → Evidence → Diagnostician → Remediator → PolicyGate
                        │            (LLM)          (LLM)      │
                        │                                        ├─ execute → Executor → PostEvidence → Verifier (LLM) → Scribe
                        │                                        ├─ refuse ──────────────────────────────────→ Scribe
                        └─ quiet ──────────────────────────────────────────────────────────────────→ Scribe
```

| Agent | Kind | Identity scope | Job |
|---|---|---|---|
| **Detector** | deterministic | `telemetry:read` | Normalize screened alert webhooks; route fired/quiet. |
| **Diagnostician** | Gemini 3.5 Flash | `telemetry:read`, `memory:read` | Root-cause over the evidence bundle (PromQL excerpts + Loki lines + incident-memory hits). |
| **Remediator** | Gemini 3.5 Flash | `actions:propose` | Select one playbook action from a closed registry. Schema-enforced — it cannot invent actions. |
| **PolicyGate** | deterministic | (framework) | Validate action params, enforce identity scopes, risk rules, approval policy. No LLM — by design. |
| **Executor** | deterministic | `actions:execute` | Idempotent dispatch to the action plane. A resumed run never double-acts. |
| **Verifier** | Gemini 3.5 Flash | `telemetry:read` | Strict post-action judgment: verified / failed / uncertain, numbers required. |
| **Scribe** | deterministic | `audit:write`, `annotations:write` | Hash-chained audit record, incident memory, Grafana annotation, postmortem. |

**The recovery story ("what if an agent loops or hallucinates?"):** a mid-incident kill
leaves the workflow checkpointed; resume continues from the last completed node, and the
action ledger refuses duplicate execution — the idempotency trap, solved. A
hallucinated action fails deterministic validation at the policy gate and dies as a
recorded refusal, never an execution. An injected prompt inside an alert webhook is
blocked by screening before any LLM sees it. All three paths are covered in the graded
evals.

## Stack

- **Google ADK 2** (`google-adk`) — graph workflow engine: routed edges, node state,
  rehydration/resume.
- **Gemini 3.5 Flash** via Gemini API / Vertex AI — the three reasoning agents.
- **Google Cloud Run** — deploy target for the service (`server.py`), plus
  Model Armor screening and Memory Bank patterns mapped onto the Gemini Enterprise
  Agent Platform component set.
- **Grafana Cloud** — live telemetry data plane: hosted Prometheus (remote-write),
  hosted Loki, unified alerting; the agents read it exactly like a human SRE would.
- **OpenTelemetry** — OTLP spans per node/agent (Cloud Trace or any OTLP backend).

## Quickstart

```bash
# 1. Python service
cd app
python -m venv .venv && .venv\Scripts\pip install -e ".[test]"   # Windows (use source .venv/bin/activate on *nix)
copy ..\.env.example ..\.env                                      # fill in Grafana + Gemini creds
.venv\Scripts\pytest                                              # test suite (no cloud creds needed)

# 2. Telemetry simulator (the night shift itself)
cd ..\sim
npm install && npm start                                          # pushes metrics+logs to Grafana Cloud

# 3. Run the service
cd ..\app
.venv\Scripts\uvicorn night_watch.server:app --port 8080

# 4. Fire an incident into the fleet
curl -X POST http://localhost:8080/alerts -H "content-type: application/json" -d @..\evals\fixtures\webhook-jam.json
curl http://localhost:8080/runs/<run_id>                          # watch it flow
```

Full cloud deployment (Cloud Run + service account steps) is in [`deploy/`](deploy/README.md).
The graded eval harness is in [`evals/`](evals/README.md) — `app\.venv\Scripts\python evals\run.py --matrix full`.

## Graded results

**9/9 scenarios pass** (run 2026-08-29, real HTTP action plane, committed artifacts in `evals/results/artifacts/`):

| Claim | Result |
|---|---|
| Happy-path detection → verified remediation | **3/3** |
| Median detect→proposal latency | **0.11 s** (n=3) |
| Prompt-injection trap refused at Armor (no LLM turn, no run) | **YES** |
| Hallucinated action (dock-9) died at policy gate — zero writes | **YES** |
| Low-confidence diagnosis refused, fault untouched | **YES** |
| Kill & resume completes with exactly ONE physical action | **2/2** |

Full per-run table + key checks: [`evals/results/report.md`](evals/results/report.md) ·
how to reproduce: [`evals/README.md`](evals/README.md).

## Lineage (rule disclosure)

Per the "new projects only" rule: this is a fresh codebase written for this hackathon —
no code is copied from any prior entry. Two reuse disclosures:

1. **Concepts** from an earlier in-house Grafana incident-response prototype
   (evidence-chain runbooks, approval-gated actions, graded evals with traps) —
   separate repo, concepts only.
2. **Data-plane component reuse:** the Grafana MCP-style telemetry client
   (`night_watch/grafana.py`) reuses the data-plane access pattern from our earlier
   hackathon Cinema entries (same authors) — hosted Prometheus/Loki query interfaces.
   It is disclosed here per the reused-components rule; all agent logic, graph,
   guards, and evals in this repo are new code written for Night Watch.

Fictional company, fictional persona, real telemetry.

## License

MIT — see [LICENSE](LICENSE).
