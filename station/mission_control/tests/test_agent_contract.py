import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from mission_control.agent_contract import (
    AGENT_CONTRACT_VERSION, AgentCapabilities, AgentIdentity, AuthenticatedAgent,
    ChannelActionEvent, HealthReport, Heartbeat, IngestedTelemetry, PrepareCommand,
    ReadinessReport, StartCommand,
    authorize_ingestion, command_from_dict, ingestion_from_dict, scenarios_compatible,
)
from mission_control.models import ChannelEvent, SatelliteEvent, StationEvent, ValidationError
from mission_control.scenarios import SCENARIO_ALLOWLIST


NOW = "2026-10-31T12:00:00Z"
RUN = "c" * 32
BASE = {"run_id": RUN, "event_time": NOW, "scenario": "baseline", "event_id": "event-1"}


def identity(agent_id="vm2-station", role="STATION"):
    return AgentIdentity(agent_id, role, "2.0.0")


class AgentContractTests(unittest.TestCase):
    def test_json_schema_is_valid_and_accepts_command(self):
        path = Path(__file__).parents[1] / "schemas" / "agent-contract-v1.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(PrepareCommand("cmd-1", RUN, "baseline", NOW).to_dict())
        caps = AgentCapabilities(identity(), tuple(sorted(SCENARIO_ALLOWLIST)))
        messages = [
            HealthReport(identity(), NOW, "HEALTHY", 10).to_dict(),
            ReadinessReport(identity(), NOW, True, "IDLE", caps).to_dict(),
            Heartbeat(identity(), "hb-1", NOW, "IDLE").to_dict(),
            ChannelActionEvent(RUN, "frame-1", "ch-1", NOW, "baseline", "FORWARD").to_dict(),
        ]
        for message in messages:
            validator.validate(message)

    def test_identity_role_is_fixed_and_versioned(self):
        self.assertEqual(identity().contract_version, AGENT_CONTRACT_VERSION)
        with self.assertRaises(ValidationError):
            identity("vm1-satellite", "STATION")
        with self.assertRaises(ValidationError):
            AgentIdentity("vm2-station", "STATION", "2.0.0", "9.0.0")

    def test_capabilities_use_exact_scenario_vocabulary(self):
        caps = AgentCapabilities(identity(), tuple(sorted(SCENARIO_ALLOWLIST)))
        self.assertTrue(scenarios_compatible(caps))
        with self.assertRaises(ValidationError):
            AgentCapabilities(identity(), ("baseline", "invented"))

    def test_commands_are_closed_and_correlated(self):
        prepare = PrepareCommand("cmd-1", RUN, "baseline", NOW)
        self.assertEqual(command_from_dict(prepare.to_dict()), prepare)
        start = StartCommand("cmd-2", RUN, "baseline", NOW, "cmd-1")
        self.assertEqual(command_from_dict(start.to_dict()), start)
        payload = prepare.to_dict()
        payload["shell"] = "echo unsafe"
        with self.assertRaises(ValidationError):
            command_from_dict(payload)

    def test_heartbeat_requires_run_correlation_while_running(self):
        with self.assertRaises(ValidationError):
            Heartbeat(identity(), "hb-1", NOW, "RUNNING")
        Heartbeat(identity(), "hb-2", NOW, "RUNNING", RUN)

    def test_channel_actions_are_explicit_and_never_decisions(self):
        forward = ChannelActionEvent(RUN, "frame:1", "ch-1", NOW, "baseline", "FORWARD")
        self.assertNotIn("decision", forward.to_dict())
        ChannelActionEvent(RUN, "frame:2", "ch-2", NOW, "jamming", "ATTACK", "JAMMING")
        with self.assertRaises(ValidationError):
            ChannelActionEvent(RUN, "frame:3", "ch-3", NOW, "baseline", "DROP", "REPLAY")

    def test_ingestion_binds_role_run_and_frame(self):
        event = ChannelEvent(**BASE, iq_bytes=10)
        message = IngestedTelemetry(identity("vm3-channel", "CHANNEL"), "frame-1", event)
        self.assertEqual(ingestion_from_dict(message.to_dict()), message)
        with self.assertRaises(ValidationError):
            IngestedTelemetry(identity("vm1-satellite", "SATELLITE"), "frame-1", event)

    def test_only_authenticated_station_peer_can_authorize_decision(self):
        event = StationEvent(**BASE, station_event_id=1, decision="ACCEPT", reason="OK",
                             rf_level="NORMAL", rf_reasons="RF_OK", iq_bytes=10,
                             satellite_id="SAT-001", session_id="session", sequence_number=1)
        message = IngestedTelemetry(identity(), "frame-1", event)
        authorize_ingestion(message, AuthenticatedAgent("vm2-station", "STATION", "mTLS", True))
        with self.assertRaises(PermissionError):
            authorize_ingestion(message, AuthenticatedAgent("vm2-station", "STATION", "mTLS", False))
        with self.assertRaises(PermissionError):
            authorize_ingestion(message, AuthenticatedAgent("vm3-channel", "CHANNEL", "mTLS", True))

    def test_non_station_cannot_smuggle_station_event(self):
        station = StationEvent(**BASE, station_event_id=2, decision="REJECT", reason="CRC_INVALID",
                               rf_level="UNKNOWN", rf_reasons="RF_METRICS_UNAVAILABLE", iq_bytes=10)
        payload = {
            "contract_version": AGENT_CONTRACT_VERSION,
            "identity": identity("vm3-channel", "CHANNEL").to_dict(),
            "frame_id": "frame-2", "event": station.to_dict(),
        }
        with self.assertRaises(ValidationError):
            ingestion_from_dict(payload)


if __name__ == "__main__":
    unittest.main()
