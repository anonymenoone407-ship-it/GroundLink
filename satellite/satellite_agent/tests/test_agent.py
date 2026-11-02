import unittest

from satellite_agent.agent import SatelliteAgent, StateError
from satellite_agent.contract import ValidationError
from satellite_agent.transmitter import OfflineTransmitterAdapter, TransmissionConfirmation

NOW = "2026-11-01T00:00:00Z"
RUN = "a" * 32
RUN2 = "b" * 32


def command(kind, command_id, run=RUN, scenario="baseline", **extra):
    value = {
        "contract_version": "1.0.0", "command_type": kind,
        "command_id": command_id, "run_id": run,
        "scenario": scenario, "issued_at": NOW,
    }
    value.update(extra)
    return value


class SatelliteAgentTests(unittest.TestCase):
    def setUp(self):
        self.tx = OfflineTransmitterAdapter()
        self.agent = SatelliteAgent(self.tx, clock=lambda: NOW)

    def prepare(self):
        return self.agent.handle_command(command("PREPARE", "p1"))

    def test_full_state_flow_and_correlations(self):
        prepared = self.prepare()
        self.assertEqual(self.agent.state, "PREPARED")
        self.agent.handle_command(command("START", "s1", preparation_id=prepared.preparation_id))
        self.assertEqual(self.agent.state, "RUNNING")
        self.agent.handle_command(command("STOP", "x1", reason="done"))
        self.assertEqual(self.agent.state, "IDLE")
        self.assertEqual([call[0] for call in self.tx.calls], ["prepare", "start", "stop"])

    def test_invalid_commands_change_nothing(self):
        unsafe = command("PREPARE", "p1")
        unsafe["shell"] = "forbidden"
        with self.assertRaises(ValidationError):
            self.agent.handle_command(unsafe)
        self.assertEqual(self.agent.state, "IDLE")
        with self.assertRaises(ValidationError):
            self.agent.handle_command(command("PREPARE", "p2", run="A" * 32))

    def test_concurrent_run_and_bad_preparation_are_rejected(self):
        prepared = self.prepare()
        with self.assertRaises(StateError):
            self.agent.handle_command(command("PREPARE", "p2", run=RUN2))
        with self.assertRaises(StateError):
            self.agent.handle_command(command(
                "START", "s-wrong-run", run=RUN2,
                preparation_id=prepared.preparation_id,
            ))
        with self.assertRaises(StateError):
            self.agent.handle_command(command(
                "START", "s-wrong-scenario", scenario="replay",
                preparation_id=prepared.preparation_id,
            ))
        with self.assertRaises(StateError):
            self.agent.handle_command(command("START", "s1", preparation_id="wrong"))
        self.assertEqual(self.agent.state, "PREPARED")
        self.assertTrue(prepared.preparation_id)

    def test_duplicate_commands_and_stop_are_idempotent(self):
        first = self.prepare()
        duplicate = self.agent.handle_command(command("PREPARE", "p1"))
        self.assertTrue(duplicate.idempotent)
        self.assertEqual(first.preparation_id, duplicate.preparation_id)
        self.agent.handle_command(command("STOP", "x1", reason="cancel"))
        stopped = self.agent.handle_command(command("STOP", "x2", reason="again"))
        self.assertEqual(stopped.state, "IDLE")
        self.assertEqual([c[0] for c in self.tx.calls].count("stop"), 1)
        with self.assertRaises(StateError):
            self.agent.handle_command(command("STOP", "x3", scenario="replay", reason="bad scope"))

    def test_command_id_collision_is_rejected(self):
        self.prepare()
        with self.assertRaises(StateError):
            self.agent.handle_command(command("PREPARE", "p1", scenario="replay"))

    def test_telemetry_exact_model_and_no_decision(self):
        prepared = self.prepare()
        self.agent.handle_command(command("START", "s1", preparation_id=prepared.preparation_id))
        envelope = self.agent.confirm_transmission(
            TransmissionConfirmation("SAT-001", "session-1", 1), frame_id="frame-1"
        )
        value = envelope.to_dict()
        self.assertEqual(value["identity"]["role"], "SATELLITE")
        self.assertEqual(value["event"]["event_type"], "satellite.telemetry_transmitted")
        forbidden = {"decision", "disposition", "attack", "iq_bytes"}
        self.assertTrue(forbidden.isdisjoint(value["event"]))

    def test_no_fabricated_transmission_before_running(self):
        with self.assertRaises(StateError):
            self.agent.confirm_transmission(
                TransmissionConfirmation("SAT-001", "session-1", 1), frame_id="frame-1"
            )

    def test_heartbeat_lifecycle_contract(self):
        self.assertEqual(self.agent.heartbeat().to_dict()["state"], "IDLE")
        self.prepare()
        heartbeat = self.agent.heartbeat().to_dict()
        self.assertEqual(heartbeat["run_id"], RUN)
        self.assertEqual(heartbeat["state"], "PREPARED")


if __name__ == "__main__":
    unittest.main()
