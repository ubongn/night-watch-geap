"""Grafana Cloud data-plane client.

Reads telemetry exactly the way a human SRE would: PromQL range queries
against hosted Prometheus and LogQL searches against hosted Loki, both via
basic auth; alert-rule state and annotation write-back via the Grafana HTTP
API with the service-account token.

This is the *read* identity only (Detector/Diagnostician/Verifier scope).
The write-back annotation call is used by the Scribe under its own scope.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import SETTINGS
from .models import Alert, LogExcerpt, MetricSeries


class GrafanaClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    # ---- Prometheus (metrics) -------------------------------------------

    async def prom_query_range(
        self, query: str, start_iso: str, end_iso: str, step_s: int = 30
    ) -> list[MetricSeries]:
        url = f"{SETTINGS.prom_query_url}/api/v1/query_range"
        auth = (SETTINGS.prom_user, SETTINGS.prom_password)
        params = {"query": query, "start": start_iso, "end": end_iso, "step": str(step_s)}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, auth=auth)
            resp.raise_for_status()
            body = resp.json()
        out: list[MetricSeries] = []
        for res in body.get("data", {}).get("result", []):
            metric = res.get("metric", {})
            values = [float(v[1]) for v in res.get("values", [])]
            out.append(MetricSeries(query=query, labels=metric, values=values))
        return out

    async def prom_instant(self, query: str) -> list[MetricSeries]:
        url = f"{SETTINGS.prom_query_url}/api/v1/query"
        auth = (SETTINGS.prom_user, SETTINGS.prom_password)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params={"query": query}, auth=auth)
            resp.raise_for_status()
            body = resp.json()
        out: list[MetricSeries] = []
        for res in body.get("data", {}).get("result", []):
            values = [float(res["value"][1])] if "value" in res else []
            out.append(MetricSeries(query=query, labels=res.get("metric", {}), values=values))
        return out

    # ---- Loki (logs) ------------------------------------------------------

    async def loki_search(self, query: str, start_iso: str, end_iso: str, limit: int = 20) -> LogExcerpt:
        url = f"{SETTINGS.loki_query_url}/loki/api/v1/query_range"
        auth = (SETTINGS.loki_user, SETTINGS.loki_password)
        params = {
            "query": query,
            "start": start_iso,
            "end": end_iso,
            "limit": str(limit),
            "direction": "backward",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, auth=auth)
            resp.raise_for_status()
            body = resp.json()
        lines: list[str] = []
        for stream in body.get("data", {}).get("result", []):
            for _, text in stream.get("values", []):
                lines.append(text[:400])
        return LogExcerpt(query=query, lines=lines, service="")

    # ---- Grafana API (alert rules + annotations) --------------------------

    async def firing_alerts(self) -> list[Alert]:
        """Firing alert instances from Grafana unified alerting."""
        url = f"{SETTINGS.grafana_url}/api/alertmanager/grafana/api/v2/alerts"
        headers = {"authorization": f"Bearer {SETTINGS.grafana_sa_token}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        alerts: list[Alert] = []
        for a in body:
            status = str(a.get("status", {}).get("state", "")).lower()
            if status not in ("alerting", "firing"):
                continue
            labels = a.get("labels", {})
            alerts.append(
                Alert(
                    name=labels.get("alertname", a.get("labels", {}).get("__name__", "unnamed")),
                    service=labels.get("service", "unknown"),
                    severity=labels.get("severity", "critical"),
                    value=float(a.get("value", 0.0) or 0.0),
                    fingerprint=a.get("fingerprint", ""),
                    labels=labels,
                )
            )
        return alerts

    async def annotate(self, text: str, tags: list[str]) -> dict[str, Any]:
        """Write an annotation back into Grafana (Scribe scope)."""
        url = f"{SETTINGS.grafana_url}/api/annotations"
        headers = {"authorization": f"Bearer {SETTINGS.grafana_sa_token}"}
        body = {"tags": tags, "text": text}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    # ---- Composite evidence fetch -----------------------------------------

    async def evidence_for(self, service: str, window_minutes: int = 20) -> dict:
        """Fetch the standard evidence pack for a service over a time window."""
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        start_iso, end_iso = start.isoformat(), end.isoformat()

        metric_queries = [
            f'avg_over_time({{__name__=~".+",service="{service}"}}[5m])',
            f'max_over_time({{__name__=~"meridian_.+",service="{service}"}}[5m])',
        ]
        log_query = f'{{service="{service}"}} |= "" '

        metrics_task = asyncio.gather(
            *[self.prom_query_range(q, start_iso, end_iso) for q in metric_queries],
            return_exceptions=True,
        )
        logs_task = self.loki_search(log_query, start_iso, end_iso)
        metrics_res, logs = await asyncio.gather(metrics_task, logs_task, return_exceptions=False)

        metrics: list[MetricSeries] = []
        for res in metrics_res:
            if isinstance(res, list):
                metrics.extend(res)
        return {"metrics": metrics, "logs": logs}
