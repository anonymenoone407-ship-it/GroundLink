"""Shared, transport-neutral contract for future GroundLink VM agents.

This module only validates messages and authenticated peer metadata.  It does
not connect to agents, execute commands, or enable LIVE mode.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, ClassVar, Mapping, Optional, Union

from .models import Event, EventType, StationEvent, ValidationError, event_from_dict
from .run_id import RunId, validate_run_id
from .scenarios import SCENARIO_ALLOWLIST, validate_scenario


AGENT_CONTRACT_VERSION = "1.0.0"
SUPPORTED_AGENT_CONTRACT_VERSIONS = frozenset({AGENT_CONTRACT_VERSION})
FRAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentRole(str, Enum):
    SATELLITE = "SATELLITE"
    CHANNEL = "CHANNEL"
    STATION = "STATION"


class AgentId(str, Enum):
    SATELLITE = "vm1-satellite"
    CHANNEL = "vm3-channel"
    STATION = "vm2-station"


ROLE_FOR_AGENT = {
    AgentId.SATELLITE.value: AgentRole.SATELLITE.value,
    AgentId.CHANNEL.value: AgentRole.CHANNEL.value,
    AgentId.STATION.value: AgentRole.STATION.value,
}


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


class AgentState(str, Enum):
    IDLE = "IDLE"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class CommandType(str, Enum):
    PREPARE = "PREPARE"
    START = "START"
    STOP = "STOP"


class ChannelDisposition(str, Enum):
    FORWARD = "FORWARD"
    DROP = "DROP"
    ATTACK = "ATTACK"


class ChannelAttack(str, Enum):
    AWGN = "AWGN"
    JAMMING = "JAMMING"
    SPOOFING = "SPOOFING"
    REPLAY = "REPLAY"
    MALFORMED = "MALFORMED"


class AuthenticationMechanism(str, Enum):
    MTLS = "mTLS"
    SIGNED_BEARER = "signed-bearer"


def _text(name: str, value: object, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value


def _time(name: str, value: object) -> str:
    text = _text(name, value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone")
    return text


def _enum(name: str, value: object, enum: type[Enum]) -> str:
    allowed = {item.value for item in enum}
    if value not in allowed:
        raise ValidationError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return str(value)


def validate_frame_id(value: object) -> str:
    if not isinstance(value, str) or FRAME_ID_PATTERN.fullmatch(value) is None:
        raise ValidationError("frame_id has an invalid format")
    return value


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: str
    software_version: str
    contract_version: str = AGENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version not in SUPPORTED_AGENT_CONTRACT_VERSIONS:
            raise ValidationError("unsupported agent contract version")
        agent_id = _enum("agent_id", self.agent_id, AgentId)
        role = _enum("role", self.role, AgentRole)
        if ROLE_FOR_AGENT[agent_id] != role:
            raise ValidationError("agent_id is not permitted for role")
        object.__setattr__(self, "software_version", _text("software_version", self.software_version, 64))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentCapabilities:
    identity: AgentIdentity
    supported_scenarios: tuple[str, ...]
    supported_commands: tuple[str, ...] = ("PREPARE", "START", "STOP")
    supported_contract_versions: tuple[str, ...] = (AGENT_CONTRACT_VERSION,)

    def __post_init__(self) -> None:
        scenarios = tuple(self.supported_scenarios)
        if not scenarios or len(set(scenarios)) != len(scenarios):
            raise ValidationError("supported_scenarios must be non-empty and unique")
        for scenario in scenarios:
            try:
                validate_scenario(scenario)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        commands = tuple(self.supported_commands)
        if set(commands) != {item.value for item in CommandType}:
            raise ValidationError("agents must support exactly PREPARE, START, and STOP")
        versions = tuple(self.supported_contract_versions)
        if AGENT_CONTRACT_VERSION not in versions or any(v not in SUPPORTED_AGENT_CONTRACT_VERSIONS for v in versions):
            raise ValidationError("capabilities do not negotiate a compatible contract version")
        object.__setattr__(self, "supported_scenarios", scenarios)
        object.__setattr__(self, "supported_commands", commands)
        object.__setattr__(self, "supported_contract_versions", versions)

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(),
                "supported_scenarios": list(self.supported_scenarios),
                "supported_commands": list(self.supported_commands),
                "supported_contract_versions": list(self.supported_contract_versions)}


@dataclass(frozen=True)
class HealthReport:
    identity: AgentIdentity
    observed_at: str
    status: str
    uptime_seconds: int
    message_type: ClassVar[str] = "agent.health"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _time("observed_at", self.observed_at))
        _enum("status", self.status, HealthStatus)
        if type(self.uptime_seconds) is not int or self.uptime_seconds < 0:
            raise ValidationError("uptime_seconds must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {"message_type": self.message_type, "contract_version": AGENT_CONTRACT_VERSION,
                "identity": self.identity.to_dict(), "observed_at": self.observed_at,
                "status": self.status, "uptime_seconds": self.uptime_seconds}


@dataclass(frozen=True)
class ReadinessReport:
    identity: AgentIdentity
    observed_at: str
    ready: bool
    state: str
    capabilities: AgentCapabilities
    reason: Optional[str] = None
    message_type: ClassVar[str] = "agent.readiness"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _time("observed_at", self.observed_at))
        if type(self.ready) is not bool:
            raise ValidationError("ready must be boolean")
        _enum("state", self.state, AgentState)
        if self.capabilities.identity != self.identity:
            raise ValidationError("readiness identity must match capabilities identity")
        if self.reason is not None:
            object.__setattr__(self, "reason", _text("reason", self.reason, 256))

    def to_dict(self) -> dict[str, Any]:
        return {"message_type": self.message_type, "contract_version": AGENT_CONTRACT_VERSION,
                "identity": self.identity.to_dict(), "observed_at": self.observed_at,
                "ready": self.ready, "state": self.state,
                "capabilities": self.capabilities.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    run_id: RunId
    scenario: str
    issued_at: str
    command_type: ClassVar[CommandType]

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _text("command_id", self.command_id))
        try:
            object.__setattr__(self, "run_id", validate_run_id(self.run_id))
            object.__setattr__(self, "scenario", validate_scenario(self.scenario))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        object.__setattr__(self, "issued_at", _time("issued_at", self.issued_at))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["contract_version"] = AGENT_CONTRACT_VERSION
        value["command_type"] = self.command_type.value
        return value


@dataclass(frozen=True)
class PrepareCommand(AgentCommand):
    command_type: ClassVar[CommandType] = CommandType.PREPARE


@dataclass(frozen=True)
class StartCommand(AgentCommand):
    preparation_id: str = ""
    command_type: ClassVar[CommandType] = CommandType.START

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "preparation_id", _text("preparation_id", self.preparation_id))


@dataclass(frozen=True)
class StopCommand(AgentCommand):
    reason: str = "operator_requested"
    command_type: ClassVar[CommandType] = CommandType.STOP

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "reason", _text("reason", self.reason, 256))


Command = Union[PrepareCommand, StartCommand, StopCommand]


@dataclass(frozen=True)
class Heartbeat:
    identity: AgentIdentity
    heartbeat_id: str
    observed_at: str
    state: str
    run_id: Optional[RunId] = None
    message_type: ClassVar[str] = "agent.heartbeat"

    def __post_init__(self) -> None:
        object.__setattr__(self, "heartbeat_id", _text("heartbeat_id", self.heartbeat_id))
        object.__setattr__(self, "observed_at", _time("observed_at", self.observed_at))
        _enum("state", self.state, AgentState)
        if self.run_id is not None:
            try:
                object.__setattr__(self, "run_id", validate_run_id(self.run_id))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        if self.state in {AgentState.PREPARED.value, AgentState.RUNNING.value, AgentState.STOPPING.value} and self.run_id is None:
            raise ValidationError("active heartbeat states require run_id")

    def to_dict(self) -> dict[str, Any]:
        return {"message_type": self.message_type, "contract_version": AGENT_CONTRACT_VERSION,
                "identity": self.identity.to_dict(), "heartbeat_id": self.heartbeat_id,
                "observed_at": self.observed_at, "state": self.state, "run_id": self.run_id}


@dataclass(frozen=True)
class ChannelActionEvent:
    run_id: RunId
    frame_id: str
    event_id: str
    event_time: str
    scenario: str
    disposition: str
    attack: Optional[str] = None
    detail: Optional[str] = None
    contract_version: str = AGENT_CONTRACT_VERSION
    message_type: ClassVar[str] = "channel.action"

    def __post_init__(self) -> None:
        if self.contract_version != AGENT_CONTRACT_VERSION:
            raise ValidationError("unsupported agent contract version")
        try:
            object.__setattr__(self, "run_id", validate_run_id(self.run_id))
            object.__setattr__(self, "scenario", validate_scenario(self.scenario))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        object.__setattr__(self, "frame_id", validate_frame_id(self.frame_id))
        object.__setattr__(self, "event_id", _text("event_id", self.event_id))
        object.__setattr__(self, "event_time", _time("event_time", self.event_time))
        disposition = _enum("disposition", self.disposition, ChannelDisposition)
        if disposition == ChannelDisposition.ATTACK.value:
            _enum("attack", self.attack, ChannelAttack)
        elif self.attack is not None:
            raise ValidationError("attack is allowed only when disposition is ATTACK")
        if self.detail is not None:
            object.__setattr__(self, "detail", _text("detail", self.detail, 256))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["message_type"] = self.message_type
        return value


@dataclass(frozen=True)
class IngestedTelemetry:
    """Correlates one existing telemetry event with a physical frame."""

    identity: AgentIdentity
    frame_id: str
    event: Event
    contract_version: str = AGENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AGENT_CONTRACT_VERSION:
            raise ValidationError("unsupported agent contract version")
        object.__setattr__(self, "frame_id", validate_frame_id(self.frame_id))
        expected = {
            AgentRole.SATELLITE.value: EventType.SATELLITE,
            AgentRole.CHANNEL.value: EventType.CHANNEL,
            AgentRole.STATION.value: EventType.STATION,
        }[self.identity.role]
        if self.event.event_type != expected:
            raise ValidationError("agent role cannot ingest this telemetry event type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "identity": self.identity.to_dict(),
            "frame_id": self.frame_id,
            "event": self.event.to_dict(),
        }


@dataclass(frozen=True)
class AuthenticatedAgent:
    """Trusted transport result; never construct this from request JSON."""

    agent_id: str
    role: str
    mechanism: str
    authenticated: bool

    def __post_init__(self) -> None:
        _enum("agent_id", self.agent_id, AgentId)
        _enum("role", self.role, AgentRole)
        _enum("mechanism", self.mechanism, AuthenticationMechanism)
        if type(self.authenticated) is not bool:
            raise ValidationError("authenticated must be boolean")


def authorize_ingestion(message: IngestedTelemetry, peer: AuthenticatedAgent) -> None:
    """Enforce peer/message binding and the Station decision trust boundary."""

    if not peer.authenticated:
        raise PermissionError("authenticated agent transport is required")
    if (peer.agent_id, peer.role) != (message.identity.agent_id, message.identity.role):
        raise PermissionError("authenticated peer does not match asserted identity")
    if isinstance(message.event, StationEvent) and peer.role != AgentRole.STATION.value:
        raise PermissionError("only authenticated Station receiver telemetry may carry a decision")


def command_from_dict(data: Mapping[str, Any]) -> Command:
    if not isinstance(data, Mapping):
        raise ValidationError("command must be an object")
    if data.get("contract_version") != AGENT_CONTRACT_VERSION:
        raise ValidationError("unsupported agent contract version")
    models = {item.command_type.value: item for item in (PrepareCommand, StartCommand, StopCommand)}
    model = models.get(data.get("command_type"))
    if model is None:
        raise ValidationError("command_type is not supported")
    allowed = set(model.__dataclass_fields__) | {"contract_version", "command_type"}
    extras = set(data) - allowed
    if extras:
        raise ValidationError(f"unexpected fields: {', '.join(sorted(extras))}")
    values = {key: value for key, value in data.items() if key not in {"contract_version", "command_type"}}
    try:
        return model(**values)
    except TypeError as exc:
        raise ValidationError(str(exc)) from exc


def ingestion_from_dict(data: Mapping[str, Any]) -> IngestedTelemetry:
    if not isinstance(data, Mapping) or set(data) != {"contract_version", "identity", "frame_id", "event"}:
        raise ValidationError("ingestion envelope has missing or unexpected fields")
    try:
        identity_data = data["identity"]
        if not isinstance(identity_data, Mapping) or set(identity_data) != {"agent_id", "role", "software_version", "contract_version"}:
            raise ValidationError("identity has missing or unexpected fields")
        return IngestedTelemetry(
            contract_version=data["contract_version"],
            identity=AgentIdentity(**identity_data),
            frame_id=data["frame_id"],
            event=event_from_dict(data["event"]),
        )
    except TypeError as exc:
        raise ValidationError(str(exc)) from exc


def scenarios_compatible(capabilities: AgentCapabilities, required: frozenset[str] = SCENARIO_ALLOWLIST) -> bool:
    """Return true only when an agent supports the Station's closed vocabulary."""

    return set(capabilities.supported_scenarios) == set(required)
