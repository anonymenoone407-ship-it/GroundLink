"""Strict wire models for the GroundLink command transport protocol v1."""

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Mapping

from .agent_contract import AgentId, AgentState, Command, CommandType, ValidationError, command_from_dict


COMMAND_PROTOCOL_VERSION = "1.0"
MAX_COMMAND_WIRE_BYTES = 16 * 1024
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _time(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValidationError(f"{name} must be a bounded timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone")
    return value


def _request_id(value: object) -> str:
    if not isinstance(value, str) or REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ValidationError("request_id has an invalid format")
    return value


@dataclass(frozen=True)
class CommandRequest:
    request_id: str
    sent_at: str
    deadline_at: str
    target_agent_id: str
    command: Command
    protocol_version: str = COMMAND_PROTOCOL_VERSION

    def __post_init__(self):
        if self.protocol_version != COMMAND_PROTOCOL_VERSION:
            raise ValidationError("unsupported command protocol version")
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        object.__setattr__(self, "sent_at", _time("sent_at", self.sent_at))
        object.__setattr__(self, "deadline_at", _time("deadline_at", self.deadline_at))
        if self.target_agent_id not in {AgentId.SATELLITE.value, AgentId.CHANNEL.value}:
            raise ValidationError("command target is not a remote agent")
        if datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00")) <= datetime.fromisoformat(self.sent_at.replace("Z", "+00:00")):
            raise ValidationError("deadline_at must follow sent_at")

    def to_dict(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "request_id": self.request_id,
                "sent_at": self.sent_at, "deadline_at": self.deadline_at,
                "target_agent_id": self.target_agent_id, "command": self.command.to_dict()}


def command_request_from_dict(data: Mapping[str, Any]) -> CommandRequest:
    if not isinstance(data, Mapping) or set(data) != {
        "protocol_version", "request_id", "sent_at", "deadline_at", "target_agent_id", "command"
    }:
        raise ValidationError("command request has missing or unexpected fields")
    return CommandRequest(
        protocol_version=data["protocol_version"], request_id=data["request_id"],
        sent_at=data["sent_at"], deadline_at=data["deadline_at"],
        target_agent_id=data["target_agent_id"], command=command_from_dict(data["command"]),
    )


@dataclass(frozen=True)
class CommandAcknowledgement:
    request_id: str
    agent_id: str
    command_id: str
    command_type: str
    run_id: str
    scenario: str
    accepted: bool
    state: str
    preparation_id: str | None
    reason: str | None
    observed_at: str
    protocol_version: str = COMMAND_PROTOCOL_VERSION

    def __post_init__(self):
        if self.protocol_version != COMMAND_PROTOCOL_VERSION:
            raise ValidationError("unsupported command protocol version")
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        if self.agent_id not in {AgentId.SATELLITE.value, AgentId.CHANNEL.value}:
            raise ValidationError("acknowledgement agent is invalid")
        if not isinstance(self.command_id, str) or not self.command_id or len(self.command_id) > 128:
            raise ValidationError("acknowledgement command_id is invalid")
        if self.command_type not in {item.value for item in CommandType}:
            raise ValidationError("acknowledgement command type is invalid")
        if type(self.accepted) is not bool or self.state not in {item.value for item in AgentState}:
            raise ValidationError("acknowledgement result is invalid")
        object.__setattr__(self, "observed_at", _time("observed_at", self.observed_at))
        # Reuse command validation for run/scenario and command-specific correlation.
        base = {"contract_version": "1.0.0", "command_type": self.command_type,
                "command_id": self.command_id, "run_id": self.run_id,
                "scenario": self.scenario, "issued_at": self.observed_at}
        if self.command_type == "START":
            base["preparation_id"] = self.preparation_id
        elif self.command_type == "STOP":
            base["reason"] = self.reason or "acknowledged"
        command_from_dict(base)
        if self.accepted and self.command_type == "PREPARE" and (
            not isinstance(self.preparation_id, str) or not self.preparation_id or len(self.preparation_id) > 128
        ):
            raise ValidationError("accepted PREPARE requires preparation_id")
        if self.command_type != "PREPARE" and self.preparation_id is not None and self.command_type != "START":
            raise ValidationError("preparation_id is not allowed for this acknowledgement")
        if not self.accepted and (not isinstance(self.reason, str) or not self.reason or len(self.reason) > 256):
            raise ValidationError("rejected acknowledgement requires a reason")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "protocol_version", "request_id", "agent_id", "command_id", "command_type",
            "run_id", "scenario", "accepted", "state", "preparation_id", "reason", "observed_at"
        )}


def command_ack_from_dict(data: Mapping[str, Any]) -> CommandAcknowledgement:
    fields = {"protocol_version", "request_id", "agent_id", "command_id", "command_type",
              "run_id", "scenario", "accepted", "state", "preparation_id", "reason", "observed_at"}
    if not isinstance(data, Mapping) or set(data) != fields:
        raise ValidationError("command acknowledgement has missing or unexpected fields")
    return CommandAcknowledgement(**{key: data[key] for key in fields})


def correlate_ack(request: CommandRequest, ack: CommandAcknowledgement) -> None:
    command = request.command
    expected = (request.request_id, request.target_agent_id, command.command_id,
                command.command_type.value, str(command.run_id), command.scenario)
    actual = (ack.request_id, ack.agent_id, ack.command_id, ack.command_type, ack.run_id, ack.scenario)
    if actual != expected:
        raise ValidationError("acknowledgement does not correlate to its command request")
    if command.command_type == CommandType.START and ack.accepted:
        if ack.state != AgentState.RUNNING.value or ack.preparation_id != command.preparation_id:
            raise ValidationError("START acknowledgement has invalid state or preparation_id")
    if command.command_type == CommandType.PREPARE and ack.accepted and ack.state != AgentState.PREPARED.value:
        raise ValidationError("PREPARE acknowledgement has invalid state")
    if command.command_type == CommandType.STOP and ack.accepted and ack.state != AgentState.IDLE.value:
        raise ValidationError("STOP acknowledgement has invalid state")

