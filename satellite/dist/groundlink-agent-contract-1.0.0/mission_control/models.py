"""Strict validated models for the Mission Control v1 telemetry contract.

These models transport observations. They do not perform cryptographic, replay,
RF, or security classification work.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import math
from typing import Any, ClassVar, Mapping, Optional, Union

from .run_id import RunId, validate_run_id
from .scenarios import validate_scenario


SCHEMA_VERSION = "1.0.0"


class ValidationError(ValueError):
    """Raised when an event does not satisfy the v1 telemetry contract."""


class EventType(str, Enum):
    SATELLITE = "satellite.telemetry_transmitted"
    CHANNEL = "channel.rf_observed"
    STATION = "station.frame_decided"


class SecurityDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class RFLevel(str, Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    ANOMALY = "ANOMALY"
    UNKNOWN = "UNKNOWN"


def _text(name: str, value: object, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value


def _timestamp(value: object) -> str:
    text = _text("event_time", value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("event_time must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("event_time must include a timezone")
    return text


def _number(name: str, value: object, *, minimum: Optional[float] = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationError(f"{name} must be a finite number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValidationError(f"{name} must be at least {minimum}")
    return result


def _integer(name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class TelemetryEvent:
    run_id: RunId
    event_time: str
    scenario: str
    event_id: str
    schema_version: str = SCHEMA_VERSION
    event_type: ClassVar[EventType]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "run_id", validate_run_id(self.run_id))
            object.__setattr__(self, "scenario", validate_scenario(self.scenario))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        object.__setattr__(self, "event_time", _timestamp(self.event_time))
        object.__setattr__(self, "event_id", _text("event_id", self.event_id, 128))
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationError(f"schema_version must be {SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["event_type"] = self.event_type.value
        return result


@dataclass(frozen=True)
class SatelliteEvent(TelemetryEvent):
    """A transmission/lifecycle fact; it can never assert a security decision."""

    satellite_id: str = ""
    session_id: str = ""
    sequence_number: int = 0
    event_type: ClassVar[EventType] = EventType.SATELLITE

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "satellite_id", _text("satellite_id", self.satellite_id, 64))
        object.__setattr__(self, "session_id", _text("session_id", self.session_id, 128))
        object.__setattr__(self, "sequence_number", _integer("sequence_number", self.sequence_number, minimum=1))


@dataclass(frozen=True)
class ChannelEvent(TelemetryEvent):
    """Unauthenticated RF-channel measurements; never a security decision."""

    iq_bytes: int = 0
    power_db_rel: Optional[float] = None
    evm_percent: Optional[float] = None
    estimated_gain: Optional[float] = None
    residual_spectral_peak_db: Optional[float] = None
    event_type: ClassVar[EventType] = EventType.CHANNEL

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "iq_bytes", _integer("iq_bytes", self.iq_bytes))
        for name in ("power_db_rel", "evm_percent", "estimated_gain", "residual_spectral_peak_db"):
            value = getattr(self, name)
            if value is not None:
                minimum = 0.0 if name in ("evm_percent", "estimated_gain") else None
                object.__setattr__(self, name, _number(name, value, minimum=minimum))


@dataclass(frozen=True)
class StationEvent(TelemetryEvent):
    """An authoritative decision copied from a real Station receiver DB row."""

    station_event_id: int = 0
    decision: str = ""
    reason: str = ""
    rf_level: str = "UNKNOWN"
    rf_reasons: str = "RF_METRICS_UNAVAILABLE"
    iq_bytes: int = 0
    processing_ms: Optional[float] = None
    satellite_id: Optional[str] = None
    session_id: Optional[str] = None
    sequence_number: Optional[int] = None
    decision_source: str = "station_receiver"
    event_type: ClassVar[EventType] = EventType.STATION

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "station_event_id", _integer("station_event_id", self.station_event_id, minimum=1))
        if self.decision not in {item.value for item in SecurityDecision}:
            raise ValidationError("decision must be ACCEPT or REJECT")
        object.__setattr__(self, "reason", _text("reason", self.reason, 256))
        if self.rf_level not in {item.value for item in RFLevel}:
            raise ValidationError("rf_level is invalid")
        object.__setattr__(self, "rf_reasons", _text("rf_reasons", self.rf_reasons, 1024))
        object.__setattr__(self, "iq_bytes", _integer("iq_bytes", self.iq_bytes))
        if self.processing_ms is not None:
            object.__setattr__(self, "processing_ms", _number("processing_ms", self.processing_ms, minimum=0.0))
        if self.decision_source != "station_receiver":
            raise ValidationError("decision_source must be station_receiver")
        for name, maximum in (("satellite_id", 64), ("session_id", 128)):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(name, value, maximum))
        if self.sequence_number is not None:
            object.__setattr__(self, "sequence_number", _integer("sequence_number", self.sequence_number, minimum=1))
        trusted_identity = (self.satellite_id, self.session_id, self.sequence_number)
        if self.decision == SecurityDecision.REJECT.value and any(value is not None for value in trusted_identity):
            raise ValidationError("rejected events must not expose an unauthenticated identity")
        if self.decision == SecurityDecision.ACCEPT.value and any(value is None for value in trusted_identity):
            raise ValidationError("accepted events require the Station-authenticated identity")


Event = Union[SatelliteEvent, ChannelEvent, StationEvent]


def event_from_dict(data: Mapping[str, Any]) -> Event:
    if not isinstance(data, Mapping):
        raise ValidationError("event must be an object")
    kind = data.get("event_type")
    models = {
        EventType.SATELLITE.value: SatelliteEvent,
        EventType.CHANNEL.value: ChannelEvent,
        EventType.STATION.value: StationEvent,
    }
    model = models.get(kind)
    if model is None:
        raise ValidationError("event_type is not supported")
    allowed = set(model.__dataclass_fields__) - {"event_type"}
    supplied = set(data) - {"event_type"}
    extras = supplied - allowed
    if extras:
        raise ValidationError(f"unexpected fields: {', '.join(sorted(extras))}")
    try:
        return model(**{key: data[key] for key in supplied})
    except TypeError as exc:
        raise ValidationError(str(exc)) from exc
