"""Environment-driven configuration for Night Watch."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass(frozen=True)
class Settings:
    # Model
    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "fake"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.5-flash"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))

    # Google Cloud
    gcp_project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    gcp_location: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_LOCATION", "europe-west1"))
    otel_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT"))
    model_armor_template: str = field(default_factory=lambda: _env("MODEL_ARMOR_TEMPLATE"))

    # Grafana data plane
    grafana_url: str = field(default_factory=lambda: _env("GRAFANA_URL"))
    grafana_sa_token: str = field(default_factory=lambda: _env("GRAFANA_SA_TOKEN"))
    prom_query_url: str = field(default_factory=lambda: _env("PROM_QUERY_URL"))
    prom_user: str = field(default_factory=lambda: _env("PROM_USER"))
    prom_password: str = field(default_factory=lambda: _env("PROM_PASSWORD"))
    loki_query_url: str = field(default_factory=lambda: _env("LOKI_QUERY_URL"))
    loki_user: str = field(default_factory=lambda: _env("LOKI_USER"))
    loki_password: str = field(default_factory=lambda: _env("LOKI_PASSWORD"))

    # Simulator action plane
    sim_base_url: str = field(default_factory=lambda: _env("SIM_BASE_URL", "http://localhost:7822"))
    sim_token: str = field(default_factory=lambda: _env("SIM_TOKEN", "night-watch-dev"))

    # Runtime
    approval_policy: str = field(default_factory=lambda: _env("APPROVAL_POLICY", "auto_approve"))
    run_state_dir: Path = field(default_factory=lambda: ROOT / _env("RUN_STATE_DIR", ".runtime"))
    audit_dir: Path = field(default_factory=lambda: ROOT / _env("AUDIT_DIR", "audit"))
    memory_dir: Path = field(default_factory=lambda: ROOT / _env("MEMORY_DIR", "memory"))


SETTINGS = Settings()
