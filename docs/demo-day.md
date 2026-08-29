# Demo Day — 4-minute video runbook

Everything below has been rehearsed locally (evidence run
`nw-20260829-081721-a9d410`, journal + hash-chained audit + action ledger committed
under `.runtime/`). The video adds exactly one thing: **the same flow running on
Google Cloud**, visible on screen.

**Recording rules**

- One take, screen + mic. 1920×1080, browser zoom ~110%.
- Terminal font ≥ 16pt. Every GCP proof moment below must be **on screen ≥ 5 s**.
- Total ≤ 4:00. The timing column is the budget; if a segment runs long, cut
  narration, not proof.

## 0. Pre-flight (before hitting record)

```bash
# GCP URL (after deploy\deploy-all.ps1 succeeded — see deploy/README.md):
gcloud run services describe night-watch --region europe-west1 --format "value(status.url)"
#   -> $APP   (e.g. https://night-watch-xxxx-uk.a.run.app)
#   -> $SIM = same command for night-watch-sim

# smoke: service is alive on GCP
curl -s $APP/healthz          # {"status":"ok",...}
```

Open three browser tabs, logged in, ready to switch:

1. **Tab A** — `$APP/` (operator dashboard, night-shift live view)
2. **Tab B** — GCP Console → Cloud Run → `night-watch` service page
3. **Tab C** — this repo README (architecture + eval table)

Terminal: one window, `night-watch-geap` checked out, ready to paste commands.

## 1. Recording order + script (timed)

| Time | Segment | What is on screen | Say (approx.) |
|---|---|---|---|
| 0:00–0:20 | **Hook** | Tab A, idle dashboard, clock showing ~02:00 night shift | "It's 2 AM at Meridian Freight. One on-call engineer, five services, zero humans awake. Night Watch is the crew that works the night shift — five agents on a Google ADK graph." |
| 0:20–0:45 | **GCP proof #1** | Tab B: Cloud Run service list showing `night-watch` + `night-watch-sim`, both green. Hover the URLs. | "The whole fleet runs on Cloud Run — this is the live service, not a laptop demo. Telemetry simulator and agent gateway are both deployed here." |
| 0:45–1:15 | **Architecture** | Tab C: agent table + graph ASCII in README | "Deterministic nodes for routing, policy, execution, audit. Gemini does the three reasoning jobs — diagnosis, proposal, verification — behind strict schemas. Every action passes an identity-scoped policy gate and an idempotency ledger." |
| 1:15–2:05 | **Live incident** | Terminal → Tab A. Paste the webhook. Watch run status flow detect→diagnose→remediate→verify→report, then dashboard shows remediated. | "Conveyor jam at dock-3, fired straight from the alert webhook. Night Watch gathers evidence from the telemetry plane, diagnoses a jam with 0.86 confidence, executes one playbook action, verifies with live numbers, files the postmortem. No human turn anywhere in that loop." |
| 2:05–2:25 | **Audit proof** | Terminal: `curl -s $APP/audit?limit=8` then `curl -s $APP/audit/verify` | "Everything is hash-chained — tamper-evident, verified here, and it's what the eval harness grades against." |
| 2:25–3:10 | **Kill & resume** | Terminal: paste the kill/resume block below. Then **GCP proof #2**: Tab B → `night-watch` service → Logs tab — show the kill + resume lines streaming. | "The failure story: we kill the fleet mid-incident. The run checkpoints. Resume continues from the last completed node — and the action ledger refuses to double-execute. One physical action, guaranteed." |
| 3:10–3:35 | **Guardrails** | Terminal: fire the injection webhook; show it refused with `blocked_at_armor` and **no run started**. | "Prompt injection inside an alert payload is screened by Model Armor logic before any LLM sees it — no run, no token spend, attempt audited." |
| 3:35–4:00 | **Close** | Tab C: eval table (9/9) | "Nine graded scenarios: happy paths, low-confidence refusals, hallucinated actions dying at the policy gate, kill-and-resume — all green. Night Watch is on GitHub, MIT-licensed. This is the night shift that lets Adaeze sleep." |

## 2. Exact commands (paste-ready)

```bash
# live incident (Tab A open while it runs)
curl -s -X POST $APP/alerts -H "content-type: application/json" \
  -d @evals/fixtures/webhook-jam.json        # -> {"run_id":"nw-..."}  (copy it)
curl -s $APP/runs/nw-XXXX | python -m json.tool

# audit chain
curl -s "$APP/audit?limit=8" | python -m json.tool
curl -s $APP/audit/verify                    # {"verified": true, ...}

# kill & resume (do it while a jam run is mid-flight; re-fire the webhook first)
curl -s -X POST $APP/runs/nw-XXXX/kill
curl -s -X POST $APP/runs/nw-XXXX/resume
curl -s $APP/runs/nw-XXXX | python -m json.tool   # status=completed, attempts=2

# prompt-injection trap (must show: blocked, no run created)
curl -s -X POST $APP/alerts -H "content-type: application/json" \
  -d @evals/fixtures/webhook-injection.json
curl -s $APP/runs                            # no new run after the blocked one
```

Rehearsal reference — `deploy/demo-kill-resume.py` runs this whole sequence
against `http://localhost:8080` and asserts the outcome (`attempts≥2`,
`outcome=remediated`, audit verified). Run it once before recording so the
narration matches reality.

## 3. GCP proof moments checklist (judges look for these)

- [ ] Cloud Run service list: both services green, URLs visible (0:20–0:45)
- [ ] Service **Logs** tab streaming during kill-and-resume (2:25–3:10)
- [ ] `/healthz` 200 from the `*.run.app` domain (pre-flight, optionally re-shown at close)
- [ ] Region visible somewhere (service detail page shows `europe-west1`) — "deployed in GCP", not a screenshot of localhost

## 4. Local evidence (fallback + b-roll)

If the cloud run flakes mid-take, the identical flow is proven locally by:

- `.runtime/run-nw-20260829-081721-a9d410.json` — full run journal, status
  `completed`, jam incident remediated end-to-end;
- `.runtime/ledger-nw-20260829-081721-a9d410.jsonl` — idempotency ledger, single
  physical action;
- `audit/` hash chain + `evals/results/report.md` — 9/9 graded matrix.

Do **not** substitute local footage for the GCP moments — the rules require the
backend visibly on GCP. Use the local run only as cutaway for the audit-chain
close-up if the live `curl` scrolls too fast.
