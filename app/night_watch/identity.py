"""Agent Identity: per-agent zero-trust capability scopes.

Every Night Watch agent runs with an explicit, minimal scope. The policy
gate consults these scopes before any action reaches the executor: the
Detector can only read telemetry, only the Remediator may propose actions,
and only the Executor (a framework component, not an LLM) can act.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Scope = str  # e.g. "telemetry:read", "actions:execute", "audit:write"


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    scopes: frozenset[Scope] = field(default_factory=frozenset)

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes


# The fleet, with least privilege baked in.
IDENTITIES: dict[str, AgentIdentity] = {
    "detector": AgentIdentity("detector", frozenset({"telemetry:read", "state:write"})),
    "diagnostician": AgentIdentity("diagnostician", frozenset({"telemetry:read", "memory:read", "state:write"})),
    "remediator": AgentIdentity("remediator", frozenset({"telemetry:read", "actions:propose", "state:write"})),
    "verifier": AgentIdentity("verifier", frozenset({"telemetry:read", "state:write"})),
    "scribe": AgentIdentity("scribe", frozenset({"audit:write", "annotations:write", "memory:write"})),
    "executor": AgentIdentity("executor", frozenset({"actions:execute"})),  # deterministic component
    "gateway": AgentIdentity("gateway", frozenset({"ingest:webhook", "armor:screen"})),
}


def get_identity(name: str) -> AgentIdentity | None:
    return IDENTITIES.get(name)
