"""Offline-only GroundLink VM3 Channel agent core."""

from .core import ChannelAgent, CommandRejected, CommandResult
from .registry import SCENARIO_REGISTRY, ScenarioBinding
from .telemetry import ExecutionObservation, TelemetryAdapter, TelemetryPair
from .script_output import ScriptOutputAdapter
from .persistence import DurableCommandProcessor
from .server import ChannelCommandServer, ServerConfig

__all__ = [
    "ChannelAgent", "CommandRejected", "CommandResult", "ExecutionObservation",
    "SCENARIO_REGISTRY", "ScenarioBinding", "ScriptOutputAdapter", "TelemetryAdapter",
    "TelemetryPair", "DurableCommandProcessor", "ChannelCommandServer", "ServerConfig",
]
