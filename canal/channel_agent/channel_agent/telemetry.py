"""Adapters from confirmed execution observations to the two v1 event streams."""

from dataclasses import dataclass
from typing import Optional

from mission_control.agent_contract import ChannelActionEvent, IngestedTelemetry
from mission_control.models import ChannelEvent, ValidationError

from .core import ChannelAgent


@dataclass(frozen=True)
class ExecutionObservation:
    """A fact emitted by a supervised script adapter, never a predicted action."""

    run_id: str
    frame_id: str
    event_id: str
    event_time: str
    scenario: str
    iq_bytes: int
    disposition: str
    attack: Optional[str] = None
    detail: Optional[str] = None
    confirmed: bool = False
    power_db_rel: Optional[float] = None
    evm_percent: Optional[float] = None
    estimated_gain: Optional[float] = None
    residual_spectral_peak_db: Optional[float] = None


@dataclass(frozen=True)
class TelemetryPair:
    telemetry: IngestedTelemetry
    action: ChannelActionEvent


class TelemetryAdapter:
    def __init__(self, agent: ChannelAgent) -> None:
        self.agent = agent

    def adapt(self, observation: ExecutionObservation) -> TelemetryPair:
        if not observation.confirmed:
            raise ValidationError("Channel action requires a confirmed execution observation")
        if self.agent.state != "RUNNING":
            raise ValidationError("Channel telemetry requires RUNNING state")
        if (observation.run_id, observation.scenario) != (self.agent.run_id, self.agent.scenario):
            raise ValidationError("observation does not match active run context")
        binding = self.agent.binding
        assert binding is not None
        if observation.disposition == "ATTACK" and observation.attack != binding.attack:
            raise ValidationError("observed attack is incompatible with prepared scenario")
        if observation.disposition != "ATTACK" and observation.attack is not None:
            raise ValidationError("non-ATTACK observation cannot name an attack")
        channel = ChannelEvent(
            run_id=observation.run_id, event_time=observation.event_time,
            scenario=observation.scenario, event_id=observation.event_id,
            iq_bytes=observation.iq_bytes, power_db_rel=observation.power_db_rel,
            evm_percent=observation.evm_percent, estimated_gain=observation.estimated_gain,
            residual_spectral_peak_db=observation.residual_spectral_peak_db,
        )
        action = ChannelActionEvent(
            run_id=observation.run_id, frame_id=observation.frame_id,
            event_id=f"{observation.event_id}:action", event_time=observation.event_time,
            scenario=observation.scenario, disposition=observation.disposition,
            attack=observation.attack, detail=observation.detail,
        )
        return TelemetryPair(
            IngestedTelemetry(self.agent.identity, observation.frame_id, channel), action
        )
