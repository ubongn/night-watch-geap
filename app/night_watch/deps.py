"""Run-scoped live dependency registry.

Live objects (Grafana async client, audit chain, memory bank, executor) must
not ride inside ADK session state — the state layer may copy or serialize it.
Instead: register them here keyed by run_id; graph nodes resolve them through
their run_id from state. The registry is process-local, which is exactly the
lifecycle of a run on one Cloud Run instance; the durable facts (audit chain,
ledger) live on disk and survive restarts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

_LOCK = threading.Lock()
_REGISTRY: dict[str, "RunDeps"] = {}


@dataclass
class RunDeps:
    grafana: Any
    audit: Any
    memory: Any
    executor: Any
    ledger: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def register(run_id: str, deps: RunDeps) -> None:
    with _LOCK:
        _REGISTRY[run_id] = deps


def resolve(run_id: str) -> RunDeps | None:
    with _LOCK:
        return _REGISTRY.get(run_id)


def drop(run_id: str) -> None:
    with _LOCK:
        _REGISTRY.pop(run_id, None)


def all_runs() -> list[str]:
    with _LOCK:
        return list(_REGISTRY)
