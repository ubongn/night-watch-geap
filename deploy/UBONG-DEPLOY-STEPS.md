# DEPLOY — exact steps for Ubong (5 minutes, console only)

**Status 2026-08-29:** everything is deploy-ready (Dockerfiles, scripts, config).
The only blocker is IAM: the staged service account
`vertex-runner@agentic-cinema-506710.iam.gserviceaccount.com` has just
"Agent Platform User" (Vertex) — it **cannot enable APIs or deploy Cloud Run**
(verified: `serviceusage.services.enable` and `run.services.list` both
PERMISSION_DENIED). Project-owner actions below are the fix. Either path works.

## Path A — you click 3 things, agent finishes (preferred)

1. **Open IAM**: <https://console.cloud.google.com/iam-admin/iam?project=agentic-cinema-506710>
   → find `vertex-runner@agentic-cinema-506710.iam.gserviceaccount.com` → pencil icon.
2. **Add these roles** (+ Add another role, 4 total):
   - Cloud Run Admin
   - Cloud Build Editor (or Service Account User + Builds Editor)
   - Artifact Registry Administrator
   - Service Account User (needed so Cloud Build/Run can act as the default compute SA)
3. **Also enable the 3 APIs** (one page: APIs & Services → Enable):
   `run.googleapis.com`, `cloudbuild.googleapis.com`, `artifactregistry.googleapis.com`
   — or ignore this, the deploy script enables them once IAM allows it.

Then tell hack_1 "roles granted" — we re-run `deploy\deploy-all.ps1` end-to-end
(SA auth already staged and working) and hand back the live URLs + health proof.

## Path B — you deploy from Cloud Shell (fully yours)

1. Console → **Cloud Shell** (terminal icon, top bar, project `agentic-cinema-506710`):

   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   git clone https://github.com/ubongn/night-watch-geap && cd night-watch-geap
   ```

2. Deploy the simulator (telemetry + action plane):

   ```bash
   gcloud run deploy night-watch-sim --source sim --region europe-west1 \
     --allow-unauthenticated --set-env-vars SIM_TOKEN=night-watch-demo
   # copy the printed URL -> $SIM
   ```

3. Deploy the agent fleet gateway, then health-check:

   ```bash
   gcloud run deploy night-watch --source app --region europe-west1 \
     --allow-unauthenticated --min-instances=1 \
     --set-env-vars SIM_BASE_URL=$SIM_URL,SIM_TOKEN=night-watch-demo,AI_PROVIDER=fake,GOOGLE_CLOUD_PROJECT=agentic-cinema-506710,GOOGLE_CLOUD_LOCATION=europe-west1

   curl $(gcloud run services describe night-watch --region europe-west1 --format 'value(status.url)')/healthz
   ```

`AI_PROVIDER=fake` = scripted deterministic turns (quota-free, same graph).
For live Gemini set `AI_PROVIDER=gemini` + `GEMINI_API_KEY=<AI Studio key>`.

**Paste the two URLs back to the team** → they go into README + demo-day doc +
the video's GCP proof moments.

## If Cloud Build fails with "permission denied ... artifactregistry"

Fresh projects sometimes have an under-privileged Cloud Build SA. Fix once:

```bash
PROJECT_NUMBER=$(gcloud projects describe agentic-cinema-506710 --format 'value(projectNumber)')
gcloud projects add-iam-policy-binding agentic-cinema-506710 \
  --member serviceAccount:$PROJECT_NUMBER@cloudbuild.gserviceaccount.com \
  --role roles/artifactregistry.writer
```

then re-run the failed `gcloud run deploy` line.
