"""Transport-neutral, offline Channel command state machine."""

from dataclasses import dataclass
from threading import RLock
from typing import Optional

from mission_control.agent_contract import (
    AgentCapabilities, AgentIdentity, AgentState, PrepareCommand, StartCommand,
    StopCommand,
)
from mission_control.scenarios import SCENARIO_ALLOWLIST

from .registry import SCENARIO_REGISTRY, ScenarioBinding


class CommandRejected(ValueError):
    """A valid contract command is invalid for the current agent context."""


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    accepted: bool
    state: str
    run_id: Optional[str]
    preparation_id: Optional[str] = None
    idempotent: bool = False


class ChannelAgent:
    """No listener and no process launcher; callers must supply validated commands."""

    identity = AgentIdentity("vm3-channel", "CHANNEL", "0.1.0")

    def __init__(self, *, recovered_state: str = "IDLE", recovered_run_id: Optional[str] = None,
                 recovered_scenario: Optional[str] = None,
                 recovered_preparation_id: Optional[str] = None) -> None:
        if frozenset(SCENARIO_REGISTRY) != SCENARIO_ALLOWLIST:
            raise RuntimeError("scenario registry does not match contract 1.0.0")
        self._lock = RLock()
        if recovered_state not in {AgentState.IDLE.value, AgentState.PREPARED.value, AgentState.ERROR.value}:
            raise ValueError("recovery permits only IDLE, PREPARED, or conservative ERROR")
        if recovered_state == AgentState.PREPARED.value and not all(
            (recovered_run_id, recovered_scenario, recovered_preparation_id)
        ):
            raise ValueError("PREPARED recovery requires complete run context")
        self._state = recovered_state
        self._run_id = recovered_run_id
        self._scenario = recovered_scenario
        self._preparation_id = recovered_preparation_id
        self._results: dict[str, tuple[object, CommandResult]] = {}
        self._last_stopped: Optional[tuple[str, str]] = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    @property
    def scenario(self) -> Optional[str]:
        return self._scenario

    @property
    def binding(self) -> Optional[ScenarioBinding]:
        return SCENARIO_REGISTRY.get(self._scenario) if self._scenario else None

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(self.identity, tuple(sorted(SCENARIO_REGISTRY)))

    def handle(self, command: object) -> CommandResult:
        if not isinstance(command, (PrepareCommand, StartCommand, StopCommand)):
            raise CommandRejected("only contract PREPARE, START, and STOP commands are accepted")
        with self._lock:
            previous = self._results.get(command.command_id)
            if previous:
                old_command, result = previous
                if old_command != command:
                    raise CommandRejected("command_id was already used for a different command")
                return CommandResult(**{**result.__dict__, "idempotent": True})
            if isinstance(command, PrepareCommand):
                result = self._prepare(command)
            elif isinstance(command, StartCommand):
                result = self._start(command)
            else:
                result = self._stop(command)
            self._results[command.command_id] = (command, result)
            return result

    def _prepare(self, command: PrepareCommand) -> CommandResult:
        if self._state != AgentState.IDLE.value:
            raise CommandRejected("a run is already prepared or active")
        if command.scenario not in SCENARIO_REGISTRY:
            raise CommandRejected("scenario is not implemented by this Channel agent")
        self._run_id, self._scenario = command.run_id, command.scenario
        self._preparation_id = command.command_id
        self._state = AgentState.PREPARED.value
        return CommandResult(command.command_id, True, self._state, self._run_id, self._preparation_id)

    def _start(self, command: StartCommand) -> CommandResult:
        if self._state != AgentState.PREPARED.value:
            raise CommandRejected("START requires PREPARED state")
        self._require_context(command.run_id, command.scenario)
        if command.preparation_id != self._preparation_id:
            raise CommandRejected("START preparation_id does not match PREPARE")
        self._state = AgentState.RUNNING.value
        return CommandResult(command.command_id, True, self._state, self._run_id, self._preparation_id)

    def _stop(self, command: StopCommand) -> CommandResult:
        if self._state == AgentState.ERROR.value:
            self._require_context(command.run_id, command.scenario)
            self._last_stopped = (command.run_id, command.scenario)
            self._state = AgentState.IDLE.value
            self._run_id = self._scenario = self._preparation_id = None
            return CommandResult(command.command_id, True, self._state, None)
        if self._state == AgentState.IDLE.value:
            if self._last_stopped == (command.run_id, command.scenario):
                return CommandResult(command.command_id, True, self._state, None, idempotent=True)
            raise CommandRejected("STOP does not match an active or previously stopped run")
        self._require_context(command.run_id, command.scenario)
        self._state = AgentState.STOPPING.value
        # Offline core has no child process. A future supervisor completes cleanup here.
        self._last_stopped = (command.run_id, command.scenario)
        self._state = AgentState.IDLE.value
        self._run_id = self._scenario = self._preparation_id = None
        return CommandResult(command.command_id, True, self._state, None)

    def _require_context(self, run_id: str, scenario: str) -> None:
        if run_id != self._run_id:
            raise CommandRejected("run_id does not match the prepared run")
        if scenario != self._scenario:
            raise CommandRejected("scenario does not match the prepared run")
