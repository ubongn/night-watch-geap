"""Optional OpenTelemetry wiring.

If OTEL_EXPORTER_OTLP_ENDPOINT is set, we install a real SDK provider with an
OTLP exporter (Cloud Trace via its OTLP endpoint, or any collector). When it
is not set, `trace.get_tracer` returns the SDK's no-op proxy — code paths
that emit spans stay identical, tests and offline runs pay nothing.

Spans today: one per gateway HTTP request (server middleware) and one per
workflow node event (runtime run loop) — i.e. per agent turn.
"""

from __future__ import annotations

import os

from opentelemetry import trace

_SETUP_DONE = False


def setup(service_name: str = "night-watch") -> bool:
    """Install the OTLP provider when an endpoint is configured. Idempotent."""
    global _SETUP_DONE
    if _SETUP_DONE:
        return True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        _SETUP_DONE = True
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": service_name, "service.namespace": "night-watch"}
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
        _SETUP_DONE = True
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break the fleet
        _SETUP_DONE = True
        return False


def tracer() -> trace.Tracer:
    """Always returns a usable tracer (no-op when unconfigured)."""
    return trace.get_tracer("night_watch")
