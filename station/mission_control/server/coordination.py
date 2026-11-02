"""Local-only readiness and command coordination model for future LIVE control.

The interfaces in this module do not connect to agents or launch processes.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import uuid

from mission_control.agent_contract import (
    AGENT_CONTRACT_VERSION, AgentId, AgentRole, AgentState, AuthenticatedAgent,
    Heartbeat, PrepareCommand, ReadinessReport, StartCommand, StopCommand,
    scenarios_compatible,
)
from mission_control.run_id import new_run_id
from mission_control.scenarios import validate_scenario


CONTROL_PROTOCOL_VERSION = "1.0"
PREPARE_ORDER = (AgentId.STATION.value, AgentId.CHANNEL.value, AgentId.SATELLITE.value)
STOP_ORDER = tuple(reversed(PREPARE_ORDER))


class CoordinationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandAck:
    command_id: str
    accepted: bool
    state: str
    preparation_id: str | None = None
    reason: str | None = None


class AgentControl(Protocol):
    async def send(self, command: PrepareCommand | StartCommand | StopCommand) -> CommandAck: ...
    async def wait_for_state(self, state: str, run_id: str, timeout: float) -> None: ...


@dataclass
class AgentReadiness:
    report: ReadinessReport
    heartbeat: Heartbeat
    protocol_version: str
    process_ready: bool
    authenticated_at: datetime


class ReadinessRegistry:
    def __init__(self, *, freshness_seconds: int = 30):
        self.freshness_seconds = freshness_seconds
        self._agents: dict[str, AgentReadiness] = {}

    @staticmethod
    def _bind(identity, peer: AuthenticatedAgent | None, local_station: bool) -> None:
        if identity.agent_id == AgentId.STATION.value:
            if not local_station or peer is not None:
                raise PermissionError("Station readiness requires trusted local provenance")
            return
        if local_station or peer is None or not peer.authenticated:
            raise PermissionError("authenticated agent readiness is required")
        if (peer.agent_id, peer.role) != (identity.agent_id, identity.role):
            raise PermissionError("readiness identity does not match authenticated peer")

    def update(
        self, report: ReadinessReport, heartbeat: Heartbeat, *,
        protocol_version: str, process_ready: bool,
        peer: AuthenticatedAgent | None = None, local_station: bool = False,
        now: datetime | None = None,
    ) -> None:
        self._bind(report.identity, peer, local_station)
        if heartbeat.identity != report.identity:
            raise CoordinationError("heartbeat identity does not match readiness")
        if protocol_version != CONTROL_PROTOCOL_VERSION:
            raise CoordinationError("control protocol is incompatible")
        if report.identity.contract_version != AGENT_CONTRACT_VERSION:
            raise CoordinationError("agent contract is incompatible")
        if not scenarios_compatible(report.capabilities):
            raise CoordinationError("agent scenario capabilities are incompatible")
        current = now or datetime.now(timezone.utc)
        report_time = datetime.fromisoformat(report.observed_at.replace("Z", "+00:00"))
        heartbeat_time = datetime.fromisoformat(heartbeat.observed_at.replace("Z", "+00:00"))
        if abs((current - report_time).total_seconds()) > self.freshness_seconds:
            raise CoordinationError("readiness report is stale")
        if abs((current - heartbeat_time).total_seconds()) > self.freshness_seconds:
            raise CoordinationError("heartbeat is stale")
        if heartbeat_time < report_time:
            raise CoordinationError("heartbeat predates readiness report")
        if report.state != heartbeat.state:
            raise CoordinationError("readiness and heartbeat states differ")
        previous = self._agents.get(report.identity.agent_id)
        if previous and heartbeat_time <= datetime.fromisoformat(previous.heartbeat.observed_at.replace("Z", "+00:00")):
            raise CoordinationError("heartbeat is stale or replayed")
        self._agents[report.identity.agent_id] = AgentReadiness(
            report, heartbeat, protocol_version, bool(process_ready), current
        )

    def get(self, agent_id: str, *, now: datetime | None = None) -> AgentReadiness | None:
        value = self._agents.get(agent_id)
        if value is None:
            return None
        current = now or datetime.now(timezone.utc)
        observed = datetime.fromisoformat(value.heartbeat.observed_at.replace("Z", "+00:00"))
        if abs((current - observed).total_seconds()) > self.freshness_seconds:
            return None
        return value

    def require_idle_ready(self, *, now: datetime | None = None) -> None:
        for agent_id in PREPARE_ORDER:
            value = self.get(agent_id, now=now)
            if value is None:
                raise CoordinationError(f"{agent_id} is NOT_READY")
            if not value.report.ready or not value.process_ready:
                raise CoordinationError(f"{agent_id} process is NOT_READY")
            if value.report.state != AgentState.IDLE.value or value.heartbeat.run_id is not None:
                raise CoordinationError(f"{agent_id} is not IDLE")


@dataclass
class CoordinationRun:
    run_id: str
    scenario: str
    state: str = "PLANNED"
    preparation_ids: dict[str, str] | None = None

    def __post_init__(self):
        if self.preparation_ids is None:
            self.preparation_ids = {}


class LiveCoordinator:
    """Deterministic orchestration against injected, authenticated clients only."""

    def __init__(self, registry: ReadinessRegistry, clients: dict[str, AgentControl], *, timeout: float = 5):
        if set(clients) != set(PREPARE_ORDER):
            raise ValueError("all three fixed control clients are required")
        self.registry = registry
        self.clients = clients
        self.timeout = timeout
        self.current: CoordinationRun | None = None
        self._lock = asyncio.Lock()

    async def prepare(self, scenario: str) -> CoordinationRun:
        scenario = validate_scenario(scenario)
        async with self._lock:
            if self.current and self.current.state != "STOPPED":
                if self.current.scenario == scenario and self.current.state in {"PREPARED", "RUNNING"}:
                    return self.current
                raise CoordinationError("another LIVE coordination run exists")
            self.registry.require_idle_ready()
            run = CoordinationRun(str(new_run_id()), scenario, "PREPARING")
            self.current = run
            prepared: list[str] = []
            try:
                for agent_id in PREPARE_ORDER:
                    command = PrepareCommand(uuid.uuid4().hex, run.run_id, scenario, datetime.now(timezone.utc).isoformat())
                    ack = await asyncio.wait_for(self.clients[agent_id].send(command), self.timeout)
                    if not ack.accepted or ack.command_id != command.command_id or ack.state != AgentState.PREPARED.value or not ack.preparation_id:
                        raise CoordinationError(f"{agent_id} rejected PREPARE")
                    run.preparation_ids[agent_id] = ack.preparation_id
                    prepared.append(agent_id)
                run.state = "PREPARED"
                return run
            except Exception as exc:
                await self._rollback(run, prepared)
                run.state = "FAILED"
                raise CoordinationError("PREPARE failed and was rolled back") from exc

    async def start(self, run_id: str) -> CoordinationRun:
        async with self._lock:
            run = self._matching(run_id)
            if run.state == "RUNNING":
                return run
            if run.state != "PREPARED":
                raise CoordinationError("PREPARE must complete before START")
            started: list[str] = []
            try:
                for agent_id in PREPARE_ORDER:
                    preparation_id = run.preparation_ids.get(agent_id)
                    if not preparation_id:
                        raise CoordinationError("missing preparation_id")
                    command = StartCommand(uuid.uuid4().hex, run.run_id, run.scenario, datetime.now(timezone.utc).isoformat(), preparation_id)
                    ack = await asyncio.wait_for(self.clients[agent_id].send(command), self.timeout)
                    if not ack.accepted or ack.command_id != command.command_id or ack.state != AgentState.RUNNING.value:
                        raise CoordinationError(f"{agent_id} rejected START")
                    await asyncio.wait_for(
                        self.clients[agent_id].wait_for_state(AgentState.RUNNING.value, run.run_id, self.timeout),
                        self.timeout,
                    )
                    started.append(agent_id)
                run.state = "RUNNING"
                return run
            except Exception as exc:
                await self._rollback(run, list(PREPARE_ORDER))
                run.state = "FAILED"
                raise CoordinationError("START failed and was rolled back") from exc

    async def stop(self, run_id: str, reason: str = "operator_requested") -> CoordinationRun:
        async with self._lock:
            run = self._matching(run_id)
            if run.state == "STOPPED":
                return run
            for agent_id in STOP_ORDER:
                command = StopCommand(uuid.uuid4().hex, run.run_id, run.scenario, datetime.now(timezone.utc).isoformat(), reason)
                try:
                    await asyncio.wait_for(self.clients[agent_id].send(command), self.timeout)
                except Exception:
                    pass
            run.state = "STOPPED"
            return run

    def _matching(self, run_id: str) -> CoordinationRun:
        if self.current is None or self.current.run_id != run_id:
            raise CoordinationError("run_id does not match coordination run")
        return self.current

    async def _rollback(self, run: CoordinationRun, agents: list[str]) -> None:
        for agent_id in reversed(agents):
            command = StopCommand(uuid.uuid4().hex, run.run_id, run.scenario, datetime.now(timezone.utc).isoformat(), "rollback")
            try:
                await asyncio.wait_for(self.clients[agent_id].send(command), self.timeout)
            except Exception:
                pass
