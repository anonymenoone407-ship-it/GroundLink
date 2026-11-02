"""Authenticated-transport payload sources; transport exposure is future work."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from .agent import SatelliteAgent
from .contract import HealthReport, ReadinessReport


class ReadinessProvider:
    def __init__(self, agent: SatelliteAgent) -> None:
        self.agent = agent
        self._started = time.monotonic()

    def observations(self, *, authenticated_peer: str) -> dict:
        if authenticated_peer != "vm2-station":
            raise PermissionError("readiness is available only to authenticated Station")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "process_online": True,
            "agent_prepared": self.agent.state == "PREPARED",
            "logical_state": self.agent.state,
            "transmitter_process_ready": False,
            "health": HealthReport(
                self.agent.identity, now, "HEALTHY", int(time.monotonic() - self._started)
            ).to_dict(),
            "readiness": ReadinessReport(
                self.agent.identity,
                now,
                False,
                self.agent.state,
                self.agent.capabilities,
                "transmitter process is not supervised",
            ).to_dict(),
            "heartbeat": self.agent.heartbeat().to_dict(),
        }
