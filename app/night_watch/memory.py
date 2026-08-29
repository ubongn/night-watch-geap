"""Incident memory — the Memory Bank lane.

Night Watch remembers resolved incidents: their fault signature, the action
that remediated them, and how verification went. The Diagnostician consults
memory ("we've seen this signature before — MTTR trending down"), and the
Scribe writes new memories after each closed incident.

Local implementation is a JSONL store. On Google Cloud this maps to the
Gemini Enterprise Agent Platform Memory Bank service; the interface is
deliberately the same (query by signature, append resolved incidents).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import IncidentRecord


def signature(service: str, fault_class: str) -> str:
    return f"{service}:{fault_class}"


class MemoryBank:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def _entries(self) -> list[dict]:
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def query(self, service: str, fault_class: str, limit: int = 3) -> list[dict]:
        sig = signature(service, fault_class)
        hits = [e for e in self._entries() if e.get("signature") == sig]
        return hits[-limit:]

    def remember(self, record: IncidentRecord) -> None:
        if record.diagnosis is None:
            return
        entry = {
            "signature": signature(record.alerts[0].service if record.alerts else "unknown",
                                   record.diagnosis.root_cause_class),
            "run_id": record.run_id,
            "outcome": record.outcome,
            "action": record.proposal.action if record.proposal else None,
            "duration_s": record.duration_s,
            "recorded_at": record.ended_at,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def stats(self) -> dict:
        entries = self._entries()
        remedi = [e for e in entries if e.get("outcome") == "remediated"]
        durs = [e.get("duration_s", 0.0) for e in remedi if e.get("duration_s")]
        return {
            "incidents_remembered": len(entries),
            "remediated": len(remedi),
            "median_mttr_s": sorted(durs)[len(durs) // 2] if durs else None,
        }
