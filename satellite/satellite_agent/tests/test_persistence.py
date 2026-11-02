import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from satellite_agent.agent import SatelliteAgent, StateError
from satellite_agent.command_protocol import TRANSPORT_ROOT, strict_request_from_dict
from satellite_agent.persistence import CommandExpired, DurableCommandProcessor
from satellite_agent.readiness import ReadinessProvider
from satellite_agent.transmitter import OfflineTransmitterAdapter

RUN = "1" * 32


class DurableProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "satellite-agent-state.sqlite3"
        self.now = datetime(2026, 11, 1, 12, 0, 1, tzinfo=timezone.utc)
        self.agent = SatelliteAgent(OfflineTransmitterAdapter())
        self.processor = DurableCommandProcessor(self.db, self.agent, now=lambda: self.now)

    def tearDown(self):
        self.processor.close()
        self.temp.cleanup()

    def request(self, kind, command_id, **extra):
        command = {
            "contract_version": "1.0.0", "command_type": kind,
            "command_id": command_id, "run_id": RUN, "scenario": "baseline",
            "issued_at": "2026-11-01T12:00:00+00:00",
        }
        command.update(extra)
        return strict_request_from_dict({
            "protocol_version": "1.0", "request_id": f"request-{command_id}",
            "sent_at": "2026-11-01T12:00:00+00:00",
            "deadline_at": "2026-11-01T12:00:05+00:00",
            "target_agent_id": "vm1-satellite", "command": command,
        })

    def test_retry_and_collision_are_durable(self):
        request = self.request("PREPARE", "prepare-1")
        first = self.processor.process(request)
        second = self.processor.process(request)
        self.assertEqual(first.preparation_id, second.preparation_id)
        collision = self.request("PREPARE", "prepare-1")
        object.__setattr__(collision.command, "scenario", "replay")
        rejected = self.processor.process(collision)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "command_id collision")

    def test_prepared_recovers_but_running_downgrades_after_restart(self):
        prepared = self.processor.process(self.request("PREPARE", "prepare-1"))
        self.processor.process(self.request(
            "START", "start-1", preparation_id=prepared.preparation_id
        ))
        self.processor.close()
        restarted_agent = SatelliteAgent(OfflineTransmitterAdapter())
        self.processor = DurableCommandProcessor(self.db, restarted_agent, now=lambda: self.now)
        self.agent = restarted_agent
        self.assertEqual(restarted_agent.state, "IDLE")
        self.assertFalse(ReadinessProvider(restarted_agent).observations(
            authenticated_peer="vm2-station"
        )["transmitter_process_ready"])

    def test_expired_request_is_rejected(self):
        self.now += timedelta(seconds=60)
        with self.assertRaises(CommandExpired):
            self.processor.process(self.request("PREPARE", "old"))

    def test_golden_prepare_request_is_wire_compatible(self):
        value = json.loads((TRANSPORT_ROOT / "vectors" / "prepare-request.json").read_text())
        parsed = strict_request_from_dict(value)
        self.assertEqual(parsed.to_dict(), value)

    def test_no_process_readiness(self):
        observations = ReadinessProvider(self.agent).observations(authenticated_peer="vm2-station")
        self.assertTrue(observations["process_online"])
        self.assertFalse(observations["transmitter_process_ready"])
        self.assertFalse(observations["readiness"]["ready"])
        with self.assertRaises(PermissionError):
            ReadinessProvider(self.agent).observations(authenticated_peer="forged")

    def test_concurrent_prepares_accept_only_one_run(self):
        other_run = "3" * 32
        first = self.request("PREPARE", "concurrent-1")
        second = self.request("PREPARE", "concurrent-2")
        object.__setattr__(second.command, "run_id", other_run)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.processor.process, (first, second)))
        self.assertEqual(sum(ack.accepted for ack in results), 1)


if __name__ == "__main__":
    unittest.main()
