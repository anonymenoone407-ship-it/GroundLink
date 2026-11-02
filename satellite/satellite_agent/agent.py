"""Transport-neutral, offline Satellite lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .contract import (
    AgentCapabilities,
    AgentIdentity,
    AgentState,
    Heartbeat,
    IngestedTelemetry,
    PrepareCommand,
    SCENARIO_ALLOWLIST,
    SatelliteEvent,
    StartCommand,
    StopCommand,
    ValidationError,
    strict_command_from_dict,
)
from .transmitter import OfflineTransmitterAdapter, TransmissionConfirmation, TransmitterAdapter

AGENT_ID = "vm1-satellite"
AGENT_ROLE = "SATELLITE"
SOFTWARE_VERSION = "0.1.0-offline"


class StateError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    command_type: str
    state: str
    run_id: str
    preparation_id: str | None = None
    idempotent: bool = False


class SatelliteAgent:
    """Single-run FSM. It exposes no listener and performs no authentication."""

    def __init__(
        self,
        transmitter: TransmitterAdapter | None = None,
        *,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.identity = AgentIdentity(AGENT_ID, AGENT_ROLE, SOFTWARE_VERSION)
        self.capabilities = AgentCapabilities(self.identity, tuple(sorted(SCENARIO_ALLOWLIST)))
        self._transmitter = transmitter or OfflineTransmitterAdapter()
        self._clock = clock
        self._started_monotonic = time.monotonic()
        self._lock = threading.RLock()
        self.state = AgentState.IDLE.value
        self.run_id: str | None = None
        self.scenario: str | None = None
        self.preparation_id: str | None = None
        self._results: dict[str, tuple[dict[str, Any], CommandResult]] = {}
        self._completed_runs: dict[str, str] = {}

    def handle_command(self, payload: Mapping[str, Any]) -> CommandResult:
        command = strict_command_from_dict(payload)
        canonical = command.to_dict()
        with self._lock:
            prior = self._results.get(command.command_id)
            if prior:
                if prior[0] != canonical:
                    raise StateError("command_id was already used with different content")
                result = prior[1]
                return CommandResult(**{**result.__dict__, "idempotent": True})
            if isinstance(command, PrepareCommand):
                result = self._prepare(command)
            elif isinstance(command, StartCommand):
                result = self._start(command)
            elif isinstance(command, StopCommand):
                result = self._stop(command)
            else:  # contract parser makes this unreachable
                raise ValidationError("unsupported command")
            self._results[command.command_id] = (canonical, result)
            return result

    def _prepare(self, command: PrepareCommand) -> CommandResult:
        if self.state != AgentState.IDLE.value:
            raise StateError("another run is already active")
        if command.run_id in self._completed_runs:
            raise StateError("completed run_id cannot be reused")
        preparation_id = uuid.uuid4().hex
        self._transmitter.prepare(command.run_id, command.scenario)
        self.run_id, self.scenario = command.run_id, command.scenario
        self.preparation_id = preparation_id
        self.state = AgentState.PREPARED.value
        return CommandResult(command.command_id, "PREPARE", self.state, command.run_id, preparation_id)

    def _require_context(self, run_id: str, scenario: str) -> None:
        if (run_id, scenario) != (self.run_id, self.scenario):
            raise StateError("command does not match active run and scenario")

    def _start(self, command: StartCommand) -> CommandResult:
        if self.state != AgentState.PREPARED.value:
            raise StateError("START requires PREPARED state")
        self._require_context(command.run_id, command.scenario)
        if command.preparation_id != self.preparation_id:
            raise StateError("preparation_id does not match active preparation")
        self._transmitter.start(command.run_id, command.scenario)
        self.state = AgentState.RUNNING.value
        return CommandResult(command.command_id, "START", self.state, command.run_id, self.preparation_id)

    def _stop(self, command: StopCommand) -> CommandResult:
        if self.state == AgentState.IDLE.value:
            completed_scenario = self._completed_runs.get(command.run_id)
            if completed_scenario is not None and completed_scenario != command.scenario:
                raise StateError("STOP scenario does not match completed run")
            # With no active run there is nothing to stop. The validated run_id
            # remains in the result so the no-op is still explicitly scoped.
            return CommandResult(command.command_id, "STOP", self.state, command.run_id)
        self._require_context(command.run_id, command.scenario)
        self.state = AgentState.STOPPING.value
        self._transmitter.request_stop(command.run_id)
        completed_run, completed_scenario = self.run_id, self.scenario
        self.state = AgentState.IDLE.value
        self.run_id = self.scenario = self.preparation_id = None
        if completed_run and completed_scenario:
            self._completed_runs[completed_run] = completed_scenario
        return CommandResult(command.command_id, "STOP", self.state, command.run_id)

    def heartbeat(self) -> Heartbeat:
        with self._lock:
            return Heartbeat(
                self.identity, uuid.uuid4().hex, self._clock(), self.state, self.run_id
            )

    def snapshot(self) -> dict[str, Any]:
        """Return persistence-safe logical state (never process readiness)."""
        with self._lock:
            return {
                "state": self.state,
                "run_id": self.run_id,
                "scenario": self.scenario,
                "preparation_id": self.preparation_id,
                "completed_runs": dict(self._completed_runs),
            }

    def restore(self, snapshot: Mapping[str, Any], *, conservative_restart: bool = True) -> None:
        """Restore durable state, demoting unverifiable running state to IDLE."""
        with self._lock:
            state = snapshot.get("state", AgentState.IDLE.value)
            run_id = snapshot.get("run_id")
            scenario = snapshot.get("scenario")
            preparation_id = snapshot.get("preparation_id")
            completed = dict(snapshot.get("completed_runs", {}))
            if conservative_restart and state in {
                AgentState.RUNNING.value, AgentState.STOPPING.value, AgentState.ERROR.value
            }:
                if run_id and scenario:
                    completed[run_id] = scenario
                state, run_id, scenario, preparation_id = AgentState.IDLE.value, None, None, None
            if state == AgentState.PREPARED.value and not all((run_id, scenario, preparation_id)):
                raise StateError("persisted PREPARED state is incomplete")
            if state == AgentState.IDLE.value:
                run_id = scenario = preparation_id = None
            self.state = state
            self.run_id, self.scenario, self.preparation_id = run_id, scenario, preparation_id
            self._completed_runs = completed

    def confirm_transmission(
        self, confirmation: TransmissionConfirmation, *, frame_id: str
    ) -> IngestedTelemetry:
        """Create exact contract telemetry only for a confirmed local IQ handoff."""
        with self._lock:
            if self.state != AgentState.RUNNING.value or not self.run_id or not self.scenario:
                raise StateError("transmission confirmation requires the active RUNNING run")
            event = SatelliteEvent(
                run_id=self.run_id,
                event_time=self._clock(),
                scenario=self.scenario,
                event_id=uuid.uuid4().hex,
                satellite_id=confirmation.satellite_id,
                session_id=confirmation.session_id,
                sequence_number=confirmation.sequence_number,
            )
            return IngestedTelemetry(self.identity, frame_id, event)
