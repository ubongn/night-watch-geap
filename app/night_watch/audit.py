"""Tamper-evident audit chain.

Every Night Watch decision — detection, diagnosis, proposal, gate, execution,
verification, scribe record — appends a record to a hash-chained JSONL file.
Each record carries the sha256 of its predecessor, so any retroactive edit
breaks the chain and `verify()` catches it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

GENESIS = "0" * 64

# One lock per chain file: appends from concurrent run-scoped AuditChain
# instances (or a restarted gateway) must serialize against the shared file.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _hash(payload: str, prev: str) -> str:
    return hashlib.sha256(f"{prev}|{payload}".encode()).hexdigest()


class AuditChain:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []
        self.head = GENESIS
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.records.append(rec)
            self.head = rec.get("hash", self.head)

    def append(self, event: str, run_id: str, data: dict | None = None) -> dict:
        with _lock_for(self.path):
            # Another instance (another run, or a restarted gateway) may have
            # appended since we loaded; re-sync before extending the chain.
            if self._disk_count() > len(self.records):
                self._load()
            payload = json.dumps(
                {"event": event, "run_id": run_id, "data": data or {}},
                sort_keys=True,
                separators=(",", ":"),
            )
            rec = {
                "event": event,
                "run_id": run_id,
                "prev": self.head,
                "hash": _hash(payload, self.head),
            }
            self.records.append({**rec, "data": data or {}})
            self.head = rec["hash"]
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**rec, "data": data or {}}, sort_keys=True) + "\n")
            return rec

    def _disk_count(self) -> int:
        if not self.path.exists():
            return 0
        n = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n += 1
        return n

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain; report the first break."""
        prev = GENESIS
        for rec in self.records:
            payload = json.dumps(
                {"event": rec["event"], "run_id": rec["run_id"], "data": rec.get("data", {})},
                sort_keys=True,
                separators=(",", ":"),
            )
            if rec.get("prev") != prev:
                return False, f"broken prev link at {rec.get('event')}"
            if rec.get("hash") != _hash(payload, prev):
                return False, f"tampered record at {rec.get('event')}"
            prev = rec["hash"]
        return True, f"chain intact ({len(self.records)} records)"

    def for_run(self, run_id: str) -> list[dict]:
        return [r for r in self.records if r.get("run_id") == run_id]
