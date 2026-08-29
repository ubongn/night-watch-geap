# Deploying Night Watch to Google Cloud Run

Two services, one graph:

| service | source | role |
|---|---|---|
| `night-watch-sim` | `sim/` | Meridian Freight night-shift simulator — telemetry data plane (`/snapshot`, `/metrics`) + authenticated control plane (the action plane the Executor writes to) |
| `night-watch` | `app/` | the five-agent fleet gateway — FastAPI (`night_watch.server:app`) |

Both build from source with Cloud Run buildpacks (no Docker needed; a `Dockerfile`
also ships for `app/` if you prefer kaniko/docker builds).

---

## One-command deploy (IAM already granted)

From the repo root, with `gcloud` authenticated and a project selected:

```powershell
# 1. the simulator (action + data plane)
gcloud run deploy night-watch-sim `
  --source sim --region europe-west1 --allow-unauthenticated `
  --set-env-vars "SIM_TOKEN=<pick-a-token>"

# 2. the fleet gateway
$SIM_URL = (gcloud run services describe night-watch-sim --region europe-west1 --format "value(status.url)")
gcloud run deploy night-watch `
  --source app --region europe-west1 --allow-unauthenticated --min-instances=1 `
  --set-env-vars "SIM_BASE_URL=$SIM_URL,SIM_TOKEN=<same-token>,AI_PROVIDER=fake,VERIFY_COOLDOWN_S=8,GOOGLE_CLOUD_PROJECT=<project>,GOOGLE_CLOUD_LOCATION=europe-west1"
```

Or run everything with defaults:

```powershell
deploy\deploy-all.ps1 -Project <PROJECT_ID> -SimToken <token> [-Region europe-west1]
```

(`--min-instances=1` keeps the kill-and-resume demo on a warm single instance;
the run journal and action ledger live on instance disk — see
[Production hardening](#production-hardening).)

### Post-deploy verification

```bash
curl $APP_URL/healthz                     # provider, sim_reachable, audit_chain.verified
curl -X POST $APP_URL/alerts -H "content-type: application/json" -d @evals/fixtures/webhook-jam.json
curl $APP_URL/runs/<run_id>               # completed / remediated
curl $APP_URL/audit/verify                # hash chain intact
```

---

## Required IAM (what the deploy identity needs)

The deploy identity (service account or user) needs on the target project:

- `roles/run.admin` — create/update Cloud Run services
- `roles/cloudbuild.builds.builder` — submit source builds
- `roles/artifactregistry.admin` — the build pushes images to AR
- `roles/iam.serviceAccountUser` — Cloud Run needs to act as the runtime service account
- `roles/serviceusage.serviceUsageAdmin` — one-time API enables (Run/Build/AR)

APIs that must be ON (console links work even without gcloud permissions):

- Cloud Run: <https://console.cloud.google.com/apis/library/run.googleapis.com>
- Cloud Build: <https://console.cloud.google.com/apis/library/cloudbuild.googleapis.com>
- Artifact Registry: <https://console.cloud.google.com/apis/library/artifactregistry.googleapis.com>

### If the hackathon requires a fresh free-trial project

1. Create the project + attach billing in the console (free trial credits apply),
   then create one service account with **Owner** (or the five roles above) and
   download its JSON key.
2. Hand the key to the build agent (or place it locally) and:
   `gcloud auth activate-service-account --key-file=<key.json>`
   `gcloud config set project <NEW_PROJECT_ID>`
3. Run `deploy\deploy-all.ps1 -Project <NEW_PROJECT_ID> -SimToken <token>` —
   it enables APIs, deploys both services, and prints the live URLs.

### Runtime LLM provider

The deployed default is `AI_PROVIDER=fake` — deterministic, free, and immune to
quota hiccups mid-demo (the graded evals run the same deterministic path). To run
the live model on GCP:

- `AI_PROVIDER=vertex` + grant `roles/aiplatform.user` to the Cloud Run runtime
  service account (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`),
  set `GEMINI_MODEL` (e.g. `gemini-2.5-flash`), or
- `AI_PROVIDER=gemini` + `GEMINI_API_KEY=<AI Studio key>`.

Everything else (screening, gate, ledger, audit) is provider-independent.

---

## Production hardening (beyond hackathon scope, documented honestly)

- **Durable state**: run journal, action ledger, audit chain and memory bank are
  JSONL on instance disk. Single warm instance (as deployed) keeps them coherent;
  multi-instance production mounts a shared volume or moves them to Cloud
  Storage/Firestore behind the same interfaces (`AuditChain`, `MemoryBank`,
  `ActionLedger` are already storage-agnostic classes).
- **Approval policy**: set `APPROVAL_POLICY=human_required` to make the gate hold
  medium/high-risk actions for a human instead of auto-approving.
- **Grafana Cloud**: set `GRAFANA_URL/PROM_*/LOKI_*` env vars and the data plane
  switches from the simulator snapshots to hosted Prometheus/Loki transparently
  (`SnapshotGrafana` vs `GrafanaClient` — same interface).
- **Model Armor**: set `MODEL_ARMOR_TEMPLATE` and screening additionally calls the
  Vertex AI Model Armor endpoint; the local heuristic screen always runs first.
