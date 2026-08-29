# Night Watch — full GCP deploy (sim + fleet gateway).
# Usage:
#   deploy\deploy-all.ps1 -Project <PROJECT_ID> -SimToken <token> [-Region europe-west1]
# Requires: gcloud authenticated with the IAM roles listed in deploy\README.md.

param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$SimToken,
    [string]$Region = "europe-west1",
    [string]$AiProvider = "fake",
    [string]$GeminiModel = ""
)

$ErrorActionPreference = "Stop"

Write-Host "== Night Watch deploy to $Project / $Region =="
gcloud config set project $Project

Write-Host "== enabling APIs (no-op if already on) =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

Write-Host "== deploying simulator (data + action plane) =="
gcloud run deploy night-watch-sim `
    --source sim --region $Region --allow-unauthenticated `
    --set-env-vars "SIM_TOKEN=$SimToken"

$SimUrl = (gcloud run services describe night-watch-sim --region $Region --format "value(status.url)").Trim()
Write-Host "sim URL: $SimUrl"

Write-Host "== deploying fleet gateway =="
$EnvVars = "SIM_BASE_URL=$SimUrl,SIM_TOKEN=$SimToken,AI_PROVIDER=$AiProvider,VERIFY_COOLDOWN_S=8,GOOGLE_CLOUD_PROJECT=$Project,GOOGLE_CLOUD_LOCATION=$Region"
if ($GeminiModel -ne "") { $EnvVars += ",GEMINI_MODEL=$GeminiModel" }

gcloud run deploy night-watch `
    --source app --region $Region --allow-unauthenticated --min-instances=1 `
    --set-env-vars $EnvVars

$AppUrl = (gcloud run services describe night-watch --region $Region --format "value(status.url)").Trim()
Write-Host ""
Write-Host "== deployed =="
Write-Host "sim:  $SimUrl"
Write-Host "app:  $AppUrl"
Write-Host ""
Write-Host "verify:  curl $AppUrl/healthz"
Write-Host "incident: curl -X POST $AppUrl/alerts -H 'content-type: application/json' -d @evals/fixtures/webhook-jam.json"
