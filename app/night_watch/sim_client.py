"""HTTP client for the simulator control plane (the action plane).

The telemetry simulator exposes a small authenticated control API:
POST /control/fault  {"kind": ..., "target": ..., "params": ...}
POST /control/repair {"target": ..., "action": ..., "params": ...}
GET  /control/faults
Night Watch's executor only ever *repairs*; fault injection is what the
eval harness and the demo use.
"""

from __future__ import annotations

import httpx

from .config import SETTINGS


class SimClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or SETTINGS.sim_base_url).rstrip("/")
        self._headers = {"authorization": f"Bearer {token or SETTINGS.sim_token}"}
        self._timeout = timeout

    async def dispatch(self, action: str, params: dict[str, str]) -> str:
        payload = {"action": action, "params": params}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/control/repair",
                json=payload,
                headers=self._headers,
            )
            resp.raise_for_status()
            body = resp.json()
            return str(body.get("detail", "ok"))

    async def inject_fault(self, kind: str, target: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/control/fault",
                json={"kind": kind, "target": target, "params": params or {}},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def clear_fault(self, target: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/control/clear",
                json={"target": target},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def faults(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.base_url}/control/faults", headers=self._headers)
            resp.raise_for_status()
            return resp.json().get("faults", [])

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/healthz")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def snapshot(self, service: str) -> dict:
        """Evidence pack for one service from the simulator data plane."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self.base_url}/snapshot", params={"service": service}
            )
            resp.raise_for_status()
            return resp.json()
